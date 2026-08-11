"""
ARTA TEA Prompt Library
All prompts used by the multi-agent system, organized by TEA layer.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 1 · REQUIREMENT ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

REQUIREMENT_EXTRACTION = """\
You are a TEA Requirement Intelligence Agent.

Extract structured requirements from this input document.

INPUT:
{document_text}

Extract ALL requirements, acceptance criteria, and constraints.

For each requirement, identify:
1. Clear title (action-oriented, ≤ 10 words)
2. Description (what the system must do)
3. Acceptance criteria (measurable conditions for "done")
4. Constraints (performance, security, compliance)
5. Domain entities involved
6. Integration dependencies (external systems, APIs)
7. Implicit requirements (unstated but industry-standard)

══════════════════════════════════════════════════════════════════════════════
MEASURABLE ACCEPTANCE CRITERIA — MANDATORY (this is the #1 quality lever)
══════════════════════════════════════════════════════════════════════════════
Every acceptance criterion's `then` MUST be objectively VERIFIABLE — a machine
must be able to assert PASS/FAIL with no human judgement. That means the `then`
contains at least ONE of:
  • an exact HTTP status code (e.g. "returns 200", "responds 403")
  • a number / threshold WITH a comparator + unit ("within 500ms", "≤ 3 retries",
    "at least 1 row", "exactly 0 errors", "> 99% of requests")
  • an exact value / enum / message ("status == 'active'", "error == 'invalid_token'")
  • a concrete field presence/shape ("response body contains `transaction_id`",
    "the `items` array is non-empty")
  • an observable state delta ("the record count increases by 1", "the `updated_at`
    timestamp changes")

And populate `measurable_threshold` with that concrete criterion.

GOOD (measurable):
  "then": "the API returns 200 and the response body contains a non-empty `token`"
  "then": "the dashboard renders within 2000ms and shows ≥ 1 insight card"
  "measurable_threshold": "HTTP 200; body.token non-empty; render < 2000ms"
BAD (vague — DO NOT emit):
  "then": "the user is logged in"            → unverifiable; replace with status+token
  "then": "the dashboard loads gracefully"   → 'gracefully' is not testable
  "then": "errors are handled properly"      → specify the exact error code/message

RULE: If a criterion cannot be made measurable, REWRITE it (derive an observable
proxy from the given/when), or SPLIT it so at least one sub-criterion is testable.
Avoid the vague words: fast, slow, good, properly, gracefully, seamless, robust,
user-friendly, appropriate, correct, as expected — replace each with a number,
status, value, or field assertion. Never leave `measurable_threshold` empty.

OUTPUT JSON (array of requirements):
[
  {{
    "id": "REQ-XXX",
    "title": "...",
    "description": "...",
    "type": "functional|non_functional|business_rule|constraint",
    "acceptance_criteria": [
      {{
        "id": "AC-XXX",
        "statement": "...",
        "given": "...",
        "when": "...",
        "then": "... (MUST be measurable — status/number/value/field assertion)",
        "measurable_threshold": "... (the concrete PASS/FAIL criterion — never empty)"
      }}
    ],
    "constraints": ["PCI-DSS", "max 3s response", ...],
    "entities": ["User", "Order", "Payment"],
    "dependencies": ["Stripe API", "Auth Service"],
    "implicit_requirements": ["GDPR data handling", ...]
  }}
]
"""

# ══════════════════════════════════════════════════════════════════════════════
# SINGLE-ISSUE EXTRACTION (Jira ticket / one user story)
# ══════════════════════════════════════════════════════════════════════════════
# A Jira issue IS one requirement — not a document to mine for "ALL requirements
# + implicit/industry-standard ones". Using the multi-requirement
# REQUIREMENT_EXTRACTION prompt on a rich epic makes the model emit a huge
# multi-requirement JSON (observed: 30K+ chars). Because the claude_code CLI
# ignores max_tokens, that unbounded generation runs >300s and trips the CLI
# timeout → 0 ACs. This focused prompt bounds the work to exactly ONE
# requirement with a handful of measurable ACs, so extraction stays fast and
# on-target for ANY SUT's Jira import.
SINGLE_REQUIREMENT_EXTRACTION = """\
You are a TEA Requirement Intelligence Agent.

The INPUT below is a SINGLE requirement (one Jira issue / user story), possibly
enriched with its linked issues, subtasks, and comments for context. Extract it
as EXACTLY ONE structured requirement.

INPUT:
{document_text}

RULES:
- Emit EXACTLY ONE requirement object (a JSON array of length 1). Do NOT split
  it into many, and do NOT invent separate "implicit/industry-standard"
  requirements.
- Emit ONE acceptance criterion for EACH distinct testable behavior the input
  describes — every operation, endpoint, state, and rule gets its own AC. A
  small story may need only 3; a broad epic (e.g. full CRUD + concurrency +
  RBAC across several endpoints) may legitimately need 10-15. Do NOT pad with
  filler, and do NOT drop a genuine behavior to hit a number — there is no
  fixed cap; the count must match the requirement's real scope. Prefer the
  read-side (view/list/search/GET) behaviors when the input is framed that way.
- Keep EACH criterion concise (one behavior, one measurable assertion). Do NOT
  restate the whole ticket; capture every testable condition compactly.

══════════════════════════════════════════════════════════════════════════════
MEASURABLE ACCEPTANCE CRITERIA — MANDATORY (this is the #1 quality lever)
══════════════════════════════════════════════════════════════════════════════
Every criterion's `then` MUST be objectively VERIFIABLE — a machine asserts
PASS/FAIL with no human judgement. The `then` contains at least ONE of:
  • an exact HTTP status code ("returns 200", "responds 403")
  • a number/threshold with comparator + unit ("within 500ms", "≥ 1 row")
  • an exact value/enum/message ("status == 'active'", "error == 'unauthorized'")
  • a concrete field presence/shape ("body contains a non-empty `organizations` array")
  • an observable state delta ("the record count increases by 1")
Populate `measurable_threshold` with that concrete criterion — never empty.
Avoid: fast, slow, good, properly, gracefully, seamless, robust, user-friendly,
appropriate, correct, as expected — replace each with a number/status/value/field.

OUTPUT JSON (array with EXACTLY ONE requirement):
[
  {{
    "id": "REQ-XXX",
    "title": "... (action-oriented, ≤ 10 words)",
    "description": "... (what the system must do)",
    "type": "functional|non_functional|business_rule|constraint",
    "acceptance_criteria": [
      {{
        "id": "AC-XXX",
        "statement": "...",
        "given": "...",
        "when": "...",
        "then": "... (MUST be measurable — status/number/value/field assertion)",
        "measurable_threshold": "... (the concrete PASS/FAIL criterion — never empty)"
      }}
    ],
    "constraints": ["..."],
    "entities": ["..."],
    "dependencies": ["..."]
  }}
]
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 2 · RISK SCORING
# ══════════════════════════════════════════════════════════════════════════════

RISK_SCORING = """\
You are a TEA Risk Analyst applying BMAD risk-based testing methodology.

Requirement to score:
{requirement_json}

Product context:
- Type: {product_type}
- User base: {user_count} users
- Revenue model: {revenue_model}
- Regulatory requirements: {regulations}
- Historical defect rate for this module: {defect_rate}%
- Days since last code change: {days_since_change}
- External dependencies: {dependencies}

BMAD TEA RISK SCORING — Two dimensions only:

IMPACT (1-3):
  1 = Minor    — cosmetic issue, workaround available, <5% users affected
  2 = Degraded — feature impaired, no workaround for some users, moderate revenue impact
  3 = Critical — total failure, data loss/breach, regulatory violation, major revenue impact

PROBABILITY (1-3):
  1 = Unlikely — mature stable code, no recent changes, strong test history
  2 = Possible — moderate complexity, some recent changes, partial test coverage
  3 = Likely   — new/rewritten code, high complexity, external deps, poor test history

FORMULA: risk_score = impact × probability → range 1-9

RISK ACTION THRESHOLDS:
  Score 1-3  → DOCUMENT (accept risk, document rationale)
  Score 4-5  → MONITOR  (actively monitor, add telemetry)
  Score 6-8  → MITIGATE (require mitigation plan with assigned owner)
  Score 9    → AUTO_FAIL (automatic quality gate FAIL — cannot ship without waiver)

PRIORITY TIERS:
  P0 = score ≥ 8 (Critical: requires 100% test coverage, zero tolerance)
  P1 = score 6-7 (High: requires 90% coverage, mitigation plan)
  P2 = score 4-5 (Medium: standard coverage targets)
  P3 = score ≤ 3 (Low: document and monitor)

Output ONLY JSON — no prose.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 3 · ACCEPTANCE TEST DESIGN
# ══════════════════════════════════════════════════════════════════════════════

GHERKIN_GENERATION = """\
You are a TEA ATDD Designer. Generate failing Gherkin scenarios (red phase).
Tests MUST be written before implementation.

Requirement: {req_id} — {req_title}
Priority: {priority} (Risk: {risk_score})
Acceptance Criterion: {ac_statement}

REQUIRED SCENARIO TYPES:
✓ Happy path — use ONLY ids listed in [HARD CONSTRAINT — REAL TEST DATA]; if an
  entity is not listed there, emit {{entity_id}} (ARTA resolves it at dispatch
  or BLOCKs truthfully). NEVER invent an id — a made-up id is a certain 404.
✓ Boundary: empty, null, whitespace, min length, max length
✓ Negative: invalid format, wrong state, missing precondition
✓ Security: SQL injection, XSS, CSRF, auth bypass (P0/P1 only)
✓ Performance: include timing assertion if AC has threshold
✓ Idempotency: if state-mutating operation
✓ Concurrency: if P0/P1 with external dependency

DATA RULES:
- Card numbers: use Stripe test numbers (4111111111111111 = valid Visa)
- Passwords: "Password123!" style (not "valid_password")
- Emails: user@test.example.com format
- Amounts: specific values like £29.99, not "an amount"

GHERKIN RULES:
- Steps must be independently verifiable
- Include Examples: tables for parameterized scenarios
- Add # REQ-XXX AC-XXX risk:P0 comments
- And/But steps for compound conditions

Output ONLY a valid .feature file. No prose.
"""

EDGE_CASE_EXPANSION = """\
You are a TEA Edge Case Specialist.

Existing scenario:
{existing_gherkin}

Generate ADDITIONAL edge cases not covered above.
Focus on:
1. Off-by-one errors (n-1, n, n+1 for any numeric boundary)
2. Character encoding (UTF-8, emoji, RTL text, null bytes)
3. Race conditions (concurrent state mutations)
4. Time-based issues (timezone, DST, leap year, clock skew)
5. Resource limits (max file size, connection pool exhaustion)
6. Dependency failures (external API timeout, database unavailable)
7. State corruption (partial updates, interrupted transactions)
8. Input fuzzing (random special characters, very long strings)

Output: Additional Scenario/Scenario Outline blocks only (no Feature header).
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 4 · AUTOMATION STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

PLAYWRIGHT_GENERATION = """\
You are a Playwright automation expert (TypeScript).

[OUTPUT SHAPE — read first]
Mode: FULL_FILE
Output: complete Playwright spec file (TypeScript).
Wrap with `import` / `test.describe(...)` / `test(...)`: YES
Output starts with: `import` keyword (first line is an import statement)
Output ends with: closing `}});` of the last test() block AND closing `}});` of test.describe
DO NOT emit: bare `test()` bodies without describe/imports; pm.test assertions;
  pytest function definitions; markdown fences (```); JSON output.

Generate a complete Playwright test file for this Gherkin scenario:

{gherkin_scenario}

Requirements:
- Use Page Object Model pattern
- data-testid selectors ONLY (most stable)
- Explicit waits using expect().toBeVisible() — NO sleep()
- Strict assertions: check text content, not just visibility
- Network request interception for API assertions
- API STATUS ASSERTIONS (mandatory): assert an HTTP status with
  `expect(resp.status()).toBe(N)`. NEVER `if (resp.status() !== N) throw new
  Error(...)` — a custom throw DROPS the numeric status from the failure
  message, so the defect classifier can't attribute the failure (it triages as
  `unknown` instead of the real 4xx/5xx). `expect().toBe()` yields Playwright's
  parseable `Expected: N / Received: M`.
- Screenshot on failure (automatic via config)
- Test isolation: use API to seed/cleanup state

Include:
- Import statements
- Page Object class (if new page)
- Full test implementation with comments linking to AC
- beforeEach for deterministic state setup
- afterEach for cleanup (API calls, not UI)

BMAD TEA FIXTURE ARCHITECTURE (canonical 3-step):
1. Write the utility as a PURE FUNCTION (unit-testable, no Playwright dependency)
2. Wrap it in a fixture via test.extend({{ fixtureName: async ({{ page }}, use) => {{ ... }} }})
3. Compose multiple fixture sets via mergeTests when one spec needs >1 capability
- NO Page Object Model inheritance chains — flat page helpers + composed fixtures
- Factory pattern for test data: createUser(), createOrder() via API
- WRONG (inline helper duplication):
    async function loginAs(page, user) {{ ... }}    // re-implemented in every spec
    test('flow', async ({{ page }}) => {{ await loginAs(page, USER); ... }});
- RIGHT (pure function → fixture → composition):
    // fixtures/auth.fixture.ts
    async function loginAs(page, user) {{ ... }}    // pure
    export const test = base.extend({{ authedPage: async ({{ page }}, use) => {{
      await loginAs(page, USER); await use(page);
    }} }});
    // spec.ts uses the authedPage fixture; combine with mergeTests if needed.

BMAD TEA NETWORK-FIRST STRATEGY (canonical: intercept BEFORE navigate):
- For ANY assertion that depends on an API response, set up the promise BEFORE
  page.goto / page.click, then `await` it BEFORE the first expect().
- Wait for responses, NOT arbitrary timeouts: page.waitForResponse()
- Use HAR recording for stable mock data: page.routeFromHAR()
- WRONG (races the network — flake source):
    await page.goto('/orders');
    await expect(page.getByTestId('order-row')).toBeVisible();   // may render before API arrives
- RIGHT (deterministic — wait for the data):
    const ordersResp = page.waitForResponse(r => r.url().includes('/api/orders') && r.ok());
    await page.goto('/orders');
    await ordersResp;                                            // data is now in the DOM
    await expect(page.getByTestId('order-row')).toBeVisible();
- For mutations (POST/PUT/DELETE): set up `waitForResponse` BEFORE the click that
  triggers the request, then await it before asserting on resulting UI changes.

BMAD TEA SELECTOR RESILIENCE HIERARCHY (in order of preference):
1. data-testid attributes — PREFERRED, most stable
2. ARIA roles/labels — getByRole(), getByLabel()
3. Text content — getByText() for visible user-facing text
4. CSS selectors — LAST RESORT, only for structural queries

FORBIDDEN SELECTORS (never use):
- .nth() index-based selection
- XPath expressions
- Class name selectors (.btn-primary, .card-header)
- ID selectors (#submit-btn) — use data-testid instead

[F1] FORBIDDEN PATTERNS — these will be rejected by the lint pass:
- if/else around assertions or page interactions (use deterministic test paths)
- try/catch around test logic (only allowed at the outermost test wrapper for cleanup)
- locator .or() chains (e.g. `page.locator('.foo').or(page.locator('.bar'))`)
- Soft assertions like `expect(x || true).toBeTruthy()` — they pass even on failure
- Generic regex selectors like `/new|create|add/i` — be specific
- `page.waitForTimeout(...)` / setTimeout / sleep — use waitFor with conditions
- Imports from local relative modules (e.g. `from './page-object-model'`,
  `from './api'`, `from './fixtures'`). Those files do NOT exist alongside
  generated specs — the only valid imports are: `@playwright/test`,
  `@playwright/test` types, and stdlib (`fs`, `path`, `crypto`, `url`).
- Define ALL Page Object classes INLINE inside the same .spec.ts file.
- Define helper API/setup functions INLINE inside the same .spec.ts file.
- Hardcoded HTTP URLs of any kind in test code or helpers. Placeholder
  hostnames (`example.com`, `example.org`, `acme.com`, `acme.app`,
  `your-app.com`, `your-domain.com`, `your-company.com`, `test.com`,
  `fake.com`, `dummy.com`) are FORBIDDEN — they fail DNS resolution at
  runtime with `getaddrinfo ENOTFOUND` and the test never reaches its
  assertions. Every API call host MUST come from `process.env.API_BASE_URL`
  (preferred) or `process.env.BASE_URL`.

[F1] REQUIRED PATTERNS:
- Every action must be preceded by `await expect(locator).toBeVisible({{ timeout: 5000 }})`
- Each acceptance criterion = exactly ONE test case with deterministic Given/When/Then steps
- Add `// AC: <ac_id>` comment on the line above each test() declaration
- Selectors: data-testid first, then role+name (getByRole), then text (getByText). NEVER CSS class.
- Use environment variables for URLs and credentials: `process.env.BASE_URL`, `process.env.TEST_USER`
- API helpers MUST derive the host from env. R112.C — DO NOT use `?? ''`
  fallback; an empty apiBase silently produces
  `apiRequestContext.post: First argument must be either URL string or
  Request` (verified live: run-d7cc3b TC-AM-018 had 4 fails from this
  exact pattern). Use the fail-fast pattern instead:
    const apiBase = process.env.API_BASE_URL || process.env.BASE_URL;
    if (!apiBase) {{
      throw new Error('[ARTA] API_BASE_URL/BASE_URL not set in test env');
    }}
    const r = await page.request.post(`${{apiBase}}/v1/users`, {{ data: {{...}} }});
  Same rule for top-level `fetch(...)` calls inside the spec.
- [F1 FIXTURE DESTRUCTURE] beforeEach/afterEach/test() callbacks that use
  `page.x` MUST destructure `{{ page }}` from the fixture argument.
  Likewise for `request`, `context`, `browser`. NEVER declare
  `let page` at describe scope and use it without assigning it.
  NEVER use `async ({{}}, use) =>` (empty destructure) when the body
  references `page` / `request`.
  WRONG (verified live in run-170e18: 16 tests crashed with
  "Cannot read properties of undefined (reading 'request')"):
    test.describe('x', () => {{
      let page: any;
      test.beforeEach(async ({{}}, use) => {{
        await page.request.post(...);   // page is undefined!
      }});
      test('case', async () => {{       // no destructure
        await page.goto('/x');
      }});
    }});
  RIGHT — destructure on every callback that uses the fixture:
    test.describe('x', () => {{
      test.beforeEach(async ({{ page }}) => {{
        await page.request.post(...);
      }});
      test('case', async ({{ page }}) => {{
        await page.goto('/x');
      }});
    }});

[F1 — ASSERTION SPECIFICITY HARD CONSTRAINT]
Assert OBSERVABLE PRESENCE, never an exact value you did NOT capture from the
SUT. Over-specified assertions are the #1 false-fail source (a real-world bulk
run showed ~125 fails from `toBe('invented')` / `toBeNull` / envelope-shape checks).
- YES: response `.ok()` / status class (2xx/4xx), a field EXISTS / is non-empty,
  an element/text is visible, a count is > 0, a known enum you captured.
- NO: an exact string / number / id / date you invented (you have not seen the
  SUT's real value) — assert the field is present + typed, not its literal value.
- NO: response-envelope SHAPE (exact keys/nesting of a JSON body) UNLESS that
  exact body was captured. WITHOUT a captured body you have NOT seen the SUT's
  real field NAMES — do NOT `toHaveProperty('<guessed-field>')` / `body.<guessed>`:
  a wrong guess FALSE-FAILS even on a 200 (the SUT may wrap in `{{responseObject,
  statusCode, …}}` while you guessed `leasableAccounts`). Assert `resp.ok()` + that
  `body` is a non-empty object/array, and STOP — do not name a key you didn't see.
- Negative tests: assert the status CLASS (`expect([400,401,403]).toContain(status)`),
  not one exact code, unless the requirement fixes it.
- SECURITY tests (SQL-injection / XSS / malicious payload): a well-built SUT SANITIZES
  the payload and returns 200 (safe handling), so assert the UNION
  `expect([200,400,401,403,404,422]).toContain(status)` — NEVER a 4xx-only array (it
  FALSE-FAILS when the SUT correctly sanitizes). Never include 5xx (a 500 on a payload
  is a real defect). The deep injection/XSS signal is ZAP's job, not this smoke check.
  WRONG:  expect(body.plan.id).toBe('PLAN-12345');          // invented id
  WRONG:  expect(body).toHaveProperty('leasableAccounts');  // GUESSED field name
  WRONG:  expect(await getToken()).toBeNull();              // guessed post-logout state
  RIGHT:  expect(resp.ok()).toBeTruthy();
          expect(body && typeof body === 'object').toBeTruthy();  // present, no guessed keys
- GUARD before .length/.map/.find on a response key you did not capture — use
  optional chaining + fallback so a missing key fails truthfully, never crashes:
  WRONG:  expect(body.items).toHaveLength(3);         // TypeError if `items` absent
  RIGHT:  expect((body?.items ?? []).length).toBeGreaterThanOrEqual(0);

[R156.H — WEBSOCKET TEST HARD CONSTRAINT]
For endpoints R156.C-classified `protocol: "websocket"` (handler decorated
with `@app.websocket(...)` OR Express `socket.on('connection', ...)`;
URL typically `/ws/...` or `wss://...`), use the canonical WS helper:

  import {{ openAuthenticatedWebSocket, expectWsMessages }} from '../common/websocket_helpers';

  test('Chat WebSocket emits welcome message', async ({{ page }}) => {{
    await page.goto('/chat');
    await waitForSPAReady(page);
    const messages = await openAuthenticatedWebSocket(
      page,
      `wss://${{SUT_HOST}}/ws/chat/connect`,
      {{ timeoutMs: 8000, maxMessages: 5, authMode: 'query' }}
    );
    await expectWsMessages(messages, m => m.length > 0, 'at least one message');
    await expectWsMessages(
      messages,
      m => m.some(x => x.type === 'received' && x.payload.includes('welcome')),
      'welcome message received',
    );
  }});

DO NOT emit raw `new WebSocket(...)` calls in test bodies — that bypasses
R156.J auto-refresh + R154 non-mutation guarantees. The helper:
  - Composes authMode='query' (token in URL query param), 'init_message'
    (token sent as first WS message after onopen), or 'none' (cookie-based
    auth via storageState)
  - Reads `process.env.AUTH_TOKEN` at call time (R95.1 + R156.J.3 propagation)
  - Enforces timeoutMs + maxMessages bounds so tests never hang
  - Returns a typed message trace for predicate-based assertions

[R156.G — SSE STREAM CONSUMER HARD CONSTRAINT]
For endpoints R156.C-classified `protocol: "sse"` (text/event-stream
Content-Type; URL typically contains `/stream`, `/events`, or
`/notifications`), use the canonical SSE helper:

  import {{ subscribeToEventStream, expectSseEvents }} from '../common/sse_helpers';

  test('Analytics stream emits expected events', async ({{ page }}) => {{
    await page.goto('/dashboard');
    await waitForSPAReady(page);
    const events = await subscribeToEventStream(
      page,
      `${{API_BASE_URL}}/v1/insight/stream?query=demo`,
      {{ timeoutMs: 15000, maxEvents: 20 }}
    );
    await expectSseEvents(events, evs => evs.length > 0, 'stream emitted events');
    await expectSseEvents(events, evs => evs.some(e => e.event === 'insight.ready'));
  }});

DO NOT use `page.waitForResponse` for SSE — that catches one HTTP
response, not the stream. The helper consumes the SSE wire-format
(`data: <payload>\\n\\n`) and returns collected events. Authorization
header is injected from `process.env.AUTH_TOKEN` (R156.J.3 + R156.B
token chain); operator can override header name + prefix via
ARTA_SSE_AUTH_HEADER_NAME / ARTA_SSE_AUTH_HEADER_PREFIX env vars.

[R156.J.2 — AUTH AUTO-REFRESH HARD CONSTRAINT]
EVERY generated test()'s `beforeEach` hook MUST call:
  await refreshAuthIfExpiring(page, request);

Import: import {{ refreshAuthIfExpiring }} from '../common/auth_refresh';

This shared helper decodes the current AUTH_TOKEN's JWT exp claim. When
TTL < threshold (default 60s) AND REFRESH_TOKEN is available, it POSTs
to the SUT's refresh endpoint (URL injected by R156.J.3 dispatcher via
ARTA_REFRESH_ENDPOINT env var) and updates AUTH_TOKEN env + the SPA's
LocalStorage. Closes the agent_token TTL exhaust class that dominates
long smokes (~110+ min wallclock vs a ~60min SUT token TTL).

When the SUT has NO refresh endpoint (R156.I.1 detected None →
ARTA_REFRESH_ENDPOINT is empty), the helper is a no-op (truthful: long
smokes will rely on operator R45.2 paste cycle; not an ARTA gen bug).

DO NOT implement refresh logic inline; ONLY call the shared helper.
DO NOT remove the import or the beforeEach call — both are required.

[R112.E — AUTH-STATE VERIFICATION HARD CONSTRAINT]
After ANY `page.goto(...)` that targets an authenticated route (anything
other than `/login`, `/signup`, `/`), verify the page actually loaded as
an authenticated user. Pre-R112.E, page.goto silently landed on the
login form when storage state cookies were stale → subsequent
`waitForResponse`/`getByRole` selectors timed out with cryptic errors
(run-d7cc3b TC-AM-001 evidence: 10s waitForResponse timeout because page
was on login screen, not dashboard). Use the canonical shared helper
(R116.B/R143.G — do NOT hand-roll body-text checks; pre-hydration HTML
and menu/footer strings false-positive them):

  import {{ skipIfAuthStale }} from '../common/sub_flows';
  ...
  await page.goto('/<authenticated-route>');  // route MUST come from this project's captured frontend routes
  // R112.E — fail-fast if auth state didn't authenticate
  await skipIfAuthStale(page);

This converts a silent 10-second timeout (FAIL) into a fast SKIP with an
actionable operator CTA. Pillar 4 mission delivery: truthful BLOCKED/
SKIPPED rows instead of false-negative test failures.

[R127.D.6.B — DO-NOT-CALL `authenticate()` HARD CONSTRAINT]
The Gherkin scenario may say "Given the user is authenticated" — this is
already handled DETERMINISTICALLY by the test runtime:
  - storageState (auth-setup.ts loads the project's session cookies +
    tokens at runtime via the playwright.base.config.ts `storageState`
    field)
  - R114.C `waitForSPAReady(page)` (post page.goto wait for SPA hydration)
  - R112.E `skipIfAuthStale(page)` (fail-fast if storage state stale)

DO NOT emit `await authenticate(page)` or `await authenticate(...)` — there
is NO `authenticate` function in the test runtime. The test will crash
with `ReferenceError: authenticate is not defined`. R123.A detects this
+ rejects the spec, but the gen-time cost is wasted retries — emit the
correct pattern on attempt 1.

If you find yourself typing `await authenticate(` — STOP. The correct
primitive is `await waitForSPAReady(page)` from `../common/sub_flows`.

BEFORE (BROKEN — observed 73× in one real-world verification trace):
  test('foo', async ({{ page }}) => {{
    await authenticate(page);  // ← undefined; ReferenceError at runtime
    await page.goto('/dashboard');
  }});

AFTER (CORRECT) — auth is preset by storageState; just goto + wait:
  test('foo', async ({{ page }}) => {{
    await page.goto('/dashboard');
    await waitForSPAReady(page);
    await skipIfAuthStale(page);
  }});

[R219.H — NO page.evaluate BEFORE NAVIGATION + NO MANUAL SESSION HARD CONSTRAINT]
`page.evaluate` runs in the BROWSER page context. Two failure modes seen on
a real-world run (38 fails):
  1. `page.evaluate(() => localStorage.clear())` (or getItem/setItem) called
     BEFORE the first `page.goto(...)` → the page is on `about:blank`, whose
     opaque origin THROWS `SecurityError: Failed to read the 'localStorage'
     property`. RULE: NEVER touch localStorage before you have navigated to a
     SUT route. The FIRST statement in every test/hook must be `page.goto(...)`.
  2. Referencing a Node-scope const inside evaluate:
     `page.evaluate(() => fetch(API_BASE + '/x'))` → `ReferenceError: API_BASE
     is not defined` (API_BASE / BASE_URL are Node vars, absent in the browser).
     RULE: pass values in — `page.evaluate((u) => fetch(u), url)` — or, for API
     contract checks, use the `{{ request }}` fixture, NOT page.evaluate+fetch
     (which also hits cross-origin SecurityError).

DO NOT manually seed/clear/read the session in localStorage
(access_token / refreshToken / idToken / activeUserName …) — storageState
already establishes the authenticated session. Manual localStorage.setItem of
tokens is BOTH unnecessary AND the #1 source of the SecurityError above.

  BEFORE (BROKEN — run-c559df pattern):
    test('x', async ({{ page }}) => {{
      await page.evaluate(() => localStorage.clear());          // SecurityError on about:blank
      await page.evaluate(() => localStorage.setItem('access_token', '...'));  // manual + crashes
      await page.goto('/portal');
    }});

  AFTER (CORRECT) — navigate first; trust storageState for the session:
    test('x', async ({{ page }}) => {{
      await page.goto('/portal');
      await waitForSPAReady(page);
      await skipIfAuthStale(page);
      // if you must READ a session value, do it AFTER navigation:
      const tok = await page.evaluate(() => localStorage.getItem('access_token'));
      expect(tok).toBeTruthy();
    }});

[R114.C — SPA-HYDRATION HARD CONSTRAINT]
After ANY `page.goto(...)` on an SPA route and BEFORE any
`expect(getByRole(...)).toBeVisible()`, you MUST wait for the SPA to
finish bootstrap. Pre-R114.C, run-0179d0 req_am_017 had 12 of 24 tests
fail with `TimeoutError: page.click: Timeout 10000ms` — the page DOM
loaded but the SPA hadn't hydrated the dashboard controls before the
10s timeout. R112.E catches the login redirect but NOT "empty shell
pre-hydration".

Import the canonical helper ONCE at the top of the spec (R116.B — do
NOT re-define it inline; a local re-definition collides with the shared
import and fails TypeScript compile, and inline copies hardcode
SUT-specific session keys):

  import {{ waitForSPAReady, skipIfAuthStale }} from '../common/sub_flows';

(The SPA session-token localStorage key is SUT-specific; the dispatcher
injects it via TARGET_SPA_TOKEN_LS_KEY and the shared helper reads it.
NEVER hardcode a session-token key inside the spec.)

Use this immediately after EVERY page.goto on an authenticated route:

  await page.goto('/<authenticated-route>');  // route from captured frontend routes
  await waitForSPAReady(page);
  await skipIfAuthStale(page);
  // Then the actual selector assertions — role+name MUST come from the DOM catalog
  await expect(page.getByRole('button', {{ name: '<name-from-DOM-catalog>' }})).toBeVisible(...);

This converts "SPA hasn't hydrated yet" (10s selector timeout) into a
"SPA hydration confirmed" check (signal-based, max 15s). When the
operator's docker network has the SUT correctly reachable, this lets
the dashboard fully load before any assertion runs.

[R115.C — VISION-ASSIST FALLBACK HARD CONSTRAINT (OPT-IN)]
When `process.env.TARGET_VISION_ASSIST === '1'` is set (operator opted
into vision-assist for THIS project), wrap critical visibility
assertions with a DOM-fast-path + vision-fallback pattern. Pre-R115.C,
when R114.C's `waitForSPAReady` exhausted its 15s budget but the
dashboard button STILL hadn't bound `aria-label` (post-hydration
re-render OR React Suspense boundary), the next selector assertion
timed out at 10s with a cryptic error — even though the button was
RENDERED VISUALLY. R115.C closes this gap.

Import at the top of the spec (always safe — helper is a no-op when
TARGET_VISION_ASSIST is unset):

  import {{ findByVision }} from '../common/vision_assist';

Apply the wrapper to assertions that have a HIGH timeout (≥10000ms)
AND target a single UI element by role+name OR testid:

In the pattern below, '<Name>' stands for a button name taken from THIS
project's DOM catalog (never invent one):

  // BEFORE (BROKEN — run-0179d0 req_am_017 pattern):
  await expect(page.getByRole('button', {{ name: '<Name>' }}))
    .toBeVisible({{ timeout: 10000 }});

  // AFTER (R115.C + R116.A — DOM fast-path → smart-locator chain → opt-in vision):
  const btn = page.getByRole('button', {{ name: '<Name>' }});
  let visible = await btn.isVisible({{ timeout: 5000 }}).catch(() => false);

  // R116.A — deterministic locator-chain fallback (zero LLM cost):
  // tries getByText → getByTestId(kebab) → fuzzy aria-label match.
  // Heals SUT label renames without operator config.
  if (!visible) {{
    visible = await smartVisible(page, 'button', '<Name>');
  }}

  // R115.C — vision-assist (opt-in via TARGET_VISION_ASSIST=1):
  if (!visible && process.env.TARGET_VISION_ASSIST === '1') {{
    const bbox = await findByVision(page, '<Name> button');
    if (bbox) {{
      console.log('[R115.C] vision-confirmed <Name> button at', bbox);
      visible = true;
    }}
  }}
  expect(visible, '<Name> button must be visible (DOM or pixel)').toBe(true);

Cost discipline: vision-assist is BEHIND a 5s DOM fast-path so
unaffected tests pay zero LLM cost. The vision call fires ≤1 per
timeout (not per action). When the project's LLM doesn't support
vision (Ollama / no provider), findByVision returns null gracefully
and the test fails on the standard assertion — never poisons
otherwise-valid tests.

[R137.A — ANALYTICS GHERKIN TRANSLATION HARD CONSTRAINT]
When a Gherkin step says `Then <field>.<sub> should be '<value>'`
(e.g., `Then insight.metric should be 'sales'`), the SUT's response
contains `insight.metric='sales'` AND the dashboard UI renders the
**VALUE** (`'sales'`), not the field name (`'insight.metric'`).

  WRONG — the literal field name will NEVER appear in the visible UI:
    await expect(page.getByText('insight.metric')).toBeVisible();
    await expect(page.getByText('insight.org_id')).toBeVisible();

  RIGHT (UI assertion — assert the rendered VALUE):
    await expect(page.getByText('sales')).toBeVisible({{ timeout: 10000 }});

  RIGHT (API assertion — when value is hard to find on screen):
    const resp = await page.waitForResponse(r => r.url().includes('/pipeline'));
    const json = await resp.json();
    expect(json.insight.metric).toEqual('sales');

This applies to ALL analytics-shape fields: `insight.*`, `narrative.*`,
`query.*`, `result.*`, `magnitude.*`. Treat the Gherkin field paths as
JSON-property paths in the SUT response — NOT as visible UI strings.

Verified live: Iter 0 of R135 produced 107 occurrences of the
literal-field-name pattern across two requirements' PW specs (75 + 32).
R126.Q's structural scorer scored these 1.000 because
parens/braces/imports were correct — but each assertion would
timeout at dispatch with "Element not found: insight.metric". R137.A
closes this semantic-translation gap at gen time.

[I6/I9] TEST DATA ISOLATION:
- Generate unique fixture identifiers per test invocation:
  `const userEmail = `test_${{Date.now()}}_${{test.info().title}}@example.com`;`
- Use beforeEach/afterEach for setup/cleanup via API (never via UI)
- NEVER hardcode http://localhost or production URLs in tests

[R132.D HARD CONSTRAINT — URL FORMAT FOR page.goto()]
ALL `page.goto(...)` arguments MUST be RELATIVE paths starting with '/'.
`playwright.config.ts` already sets `baseURL` from TARGET_BASE_URL env
var; Playwright automatically prepends it. Inside docker-compose,
`localhost` resolves to the chromium container itself (NOT the host),
so any hardcoded `http://localhost:NNNN` URL causes
ERR_CONNECTION_REFUSED at dispatch — regardless of what dev-fallback
you wrap it in.

CORRECT:
  await page.goto('/dashboard');
  await page.goto('/projects/123/edit');

FORBIDDEN — these all trigger R24a validator rejection at gen time:
  await page.goto('http://localhost:3000/dashboard');
  await page.goto('http://127.0.0.1:3000/dashboard');
  await page.goto(`${{process.env.TARGET_BASE_URL ?? 'http://localhost:3000'}}/dashboard`);

Verified live: one requirement's spec exhausted 3 retries × multiple regen
attempts because the LLM kept producing the third FORBIDDEN form
(localhost as dev-fallback in a template literal). R132.D removes the
fallback path entirely — use relative paths.

[R77.7.A] FLAKE RESILIENCE — required patterns for ≥95% effective pass rate:
- At the TOP of every `test.describe(...)` block, configure retries:
    test.describe('Feature: <name>', () => {{
      test.describe.configure({{ retries: 2 }});
      ...
    }});
  Playwright config retries to 1 locally / 2 in CI; spec-level configure
  ensures every generated test gets a chance to recover from network/
  timing flakes. The 2-retry budget catches the common flake classes
  (request races, intermittent SPA hydration) without masking real bugs
  (a genuinely broken test fails on all 3 attempts).
- EVERY `expect(...)` matcher that asserts on a UI value MUST use
  auto-retrying form with explicit timeout:
    await expect(page.getByTestId('total')).toHaveText('$42.00', {{ timeout: 10000 }});
    await expect(page.getByTestId('row-1')).toBeVisible({{ timeout: 10000 }});
  Without {{ timeout }}, Playwright's default (5s) may miss SPA hydration
  on slower environments. 10s gives breathing room without being absurd.
- After `page.goto(...)` AND before the first `expect(...)` that depends
  on SPA-rendered content, prefer ONE of:
    (a) waitForResponse for the specific API the page needs:
        const ordersResp = page.waitForResponse(r => r.url().includes('/api/orders'));
        await page.goto('/orders');
        await ordersResp;
    (b) waitForLoadState('networkidle') with a short upper bound:
        await page.goto('/orders');
        await page.waitForLoadState('networkidle', {{ timeout: 5000 }});
  The networkidle wait is acceptable when the page makes many requests
  and pinning to one would be brittle. NEVER use waitForTimeout(...) for
  this — it's a hardcoded sleep that hides real timing bugs.
- For interactive flows (click → API call → DOM update), use the
  network-first pattern (see BMAD TEA NETWORK-FIRST STRATEGY above):
  set up `waitForResponse` BEFORE the click, then await it BEFORE
  asserting on the UI change.

Output: Complete TypeScript file, no prose. Return ONLY the raw code — do NOT wrap in markdown code fences (no ```). No introductory text, no explanations.
"""

ACCESSIBILITY_GENERATION = """\
You are a web accessibility (WCAG 2.1 AA) test engineer using Playwright + axe-core.

Generate a complete Playwright accessibility test file for this requirement.

GHERKIN / REQUIREMENT:
{gherkin_scenario}

[F3-3] HARD REQUIREMENTS:
- Import: `import {{ test, expect }} from '@playwright/test';`
  `import {{ injectAxe, checkA11y, getViolations }} from 'axe-playwright';`
  `import {{ waitForSPAReady, skipIfAuthStale }} from '../common/sub_flows';`
- In beforeEach, navigate to a REAL authenticated route from the GROUNDED ROUTES
  block below (NOT just '/', which renders the login/selection shell and yields a
  VACUOUS scan), then wait for hydration + bail truthfully if auth is stale, then
  inject axe:
  `await page.goto(`${{process.env.BASE_URL}}<a-real-route-from-the-grounded-list>`);`
  `await waitForSPAReady(page); await skipIfAuthStale(page); await injectAxe(page);`
- [R112.E AUTH VERIFY] In EVERY test, BEFORE `checkA11y`, assert you are on a real
  authenticated page (not the login/selection wall) — otherwise the WCAG result is
  meaningless: `await expect(page).not.toHaveURL(/\\/(login|signin|auth)(\\/|$|\\?)/);`
- Each user-visible page/state from the requirement = ONE test (navigating a REAL
  grounded route) that calls
  `await checkA11y(page, null, {{ detailedReport: true, axeOptions: {{ runOnly: ['wcag2a', 'wcag2aa'] }} }});`
- Every test must also assert violation count: `const violations = await getViolations(page); expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);`
- Tag the file with `test.describe('Accessibility — <REQ-ID>', () => {{ ... }})`
- Add `// AC: <ac_id>` comment above each test() declaration
- [F5-1] Define this SHARED helper EXACTLY ONCE, after the imports and BEFORE
  `test.describe`, so the run-wide JSON report the ARTA gate reads
  (`nfr.a11y_violations_critical/moderate`) is written without repeating the
  block in every test (repetition bloats the file and TRUNCATES it):
  ```ts
  function recordA11y(violations: any[]) {{
    // Fail loudly if A11Y_REPORT_PATH is unset — silently skipping the write
    // means the gate sees zero violations and passes a regression.
    if (!process.env.A11Y_REPORT_PATH) {{
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently.');
    }}
    const fs = require('fs');
    let prior: any[] = [];
    try {{ prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); }} catch {{}}
    fs.writeFileSync(process.env.A11Y_REPORT_PATH,
      JSON.stringify([...prior, ...violations.map(v => ({{
        id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl,
        description: v.description,
        tags: (v.tags || []).filter((t) => /wcag|best-practice/.test(t)),
        nodes: (v.nodes || []).slice(0, 5).map((n) => ({{
          target: n.target, html: (n.html || '').slice(0, 200), failureSummary: n.failureSummary
        }})),
      }}))], null, 2));
  }}
  ```
- In EACH test, immediately after `getViolations(page)` and BEFORE the expect,
  call the helper in ONE line — do NOT inline the fs write per test:
  `recordA11y(violations);`

[F3-3] FORBIDDEN:
- Soft assertions on violation count (must be hard equality to 0)
- Skipping rules without a {{rationale}} comment cite
- waitForTimeout/sleep — use waitFor conditions

OUTPUT: Complete TypeScript file. No markdown fences. No prose.
"""

APPIUM_GENERATION = """\
You are an Appium 2.x mobile testing expert using Python.

Generate a complete Appium test file for this Gherkin scenario:

{gherkin_scenario}

Requirements:
- Use Appium 2.x with Python client (appium-python-client)
- Page Object Model pattern — separate page class from test class
- Use WebDriverWait with expected_conditions for all element interactions
- Support both Android (UiAutomator2) and iOS (XCUITest) via capability abstraction
- Capabilities loaded from environment variables or config dict
- pytest fixtures for driver setup/teardown
- Meaningful assertions with descriptive messages
- Screenshot capture on failure via pytest hook
- Touch actions for swipe/scroll where applicable

Output: Complete Python test file. No prose. Return ONLY the raw code — do NOT wrap in markdown code fences (no ```). No introductory text, no explanations.
"""

TEST_GENERATION_QUALITY_RULES = """
## MANDATORY TEST QUALITY RULES (BMAD TEA Standard)

1. NO HARD WAITS: Use `expect().toBeVisible()`, `waitForResponse()`, `waitForLoadState()`. NEVER use `sleep()`, `waitForTimeout()`, or fixed delays.

2. NO CONDITIONALS IN TEST FLOW: Each test must follow a single deterministic path. No `if/else` branching. Use separate test cases for different scenarios.

3. CONCISE: Each spec file must be under 300 lines. Split into multiple files if needed.

4. FAST: Each test must complete in under 90 seconds.

5. SELF-CLEANING: Use API calls in `beforeEach` to seed test data. Use API calls in `afterEach` to delete test data. Never rely on UI for setup/teardown.

6. VISIBLE ASSERTIONS: Every test must have at least 2 explicit `expect()` calls. Assert both the action and its side effect.

7. UNIQUE DATA: Use `crypto.randomUUID()` or `Date.now()` ONLY for identifiers of entities this test CREATES. For any entity the test READS/UPDATES/DELETES, use a real id from [HARD CONSTRAINT — REAL TEST DATA] or emit `{{entity_id}}` — a random id on a GET is a guaranteed 404. Never use static data that could conflict with parallel tests.

8. PARALLEL-SAFE: No shared state between tests. Each test creates and destroys its own data.

## EDGE CASE COVERAGE (generate for every requirement)

For each requirement, generate tests covering:
- Boundary values: empty strings, max-length strings (10000+ chars), zero, negative numbers
- Invalid input: wrong types, missing required fields, SQL injection (`'; DROP TABLE --`), XSS (`<script>alert('xss')</script>`)
- Unauthorized access: wrong role, expired token, no auth header
- Not found: invalid UUIDs, deleted resources
- Duplicate operations: creating same resource twice (expect 409)
- Concurrent operations: simultaneous updates (use Promise.all)

## SELECTOR HIERARCHY (strict order)

1. data-testid attributes (PREFERRED): `page.getByTestId('submit-btn')`
2. ARIA roles/labels: `page.getByRole('button', {{ name: 'Submit' }})`
3. Text content: `page.getByText('Submit')`
4. CSS selectors: `page.locator('.submit-button')` (LAST RESORT)

FORBIDDEN: .nth() index-based, XPath, class-only selectors, ID selectors
"""

MULTI_ROLE_TEST_TEMPLATE = """
## ROLE-BASED ACCESS CONTROL TESTS

For apps with multiple user roles, generate:

1. POSITIVE TESTS (per role):
   - Admin: can perform all operations (CRUD + delete + manage users)
   - Developer: can create and update own resources
   - Tester: can view and create bug reports

2. NEGATIVE TESTS (cross-role restrictions):
   - Tester CANNOT delete users → expect 403
   - Developer CANNOT access admin endpoints → expect 403
   - Unauthenticated user CANNOT access protected routes → expect 401 or redirect to /login

3. IMPLEMENTATION PATTERN:
```typescript
test.describe('Admin operations', () => {{
  test.use({{ storageState: process.env.AUTH_STATE_ADMIN || '.arta/auth-state-admin.json' }});
  test('can delete user', async ({{ request }}) => {{
    const resp = await request.delete(`${{API}}/api/users/${{userId}}`);
    expect(resp.status()).toBe(200);
  }});
}});

test.describe('Tester restrictions', () => {{
  test.use({{ storageState: process.env.AUTH_STATE_TESTER || '.arta/auth-state-tester.json' }});
  test('cannot delete user — expects 403', async ({{ request }}) => {{
    const resp = await request.delete(`${{API}}/api/users/${{userId}}`);
    expect(resp.status()).toBe(403);
  }});
}});
```
"""

TEST_QUALITY_REMEDIATION = """\
You are a BMAD TEA Test Quality Remediator.

The following test script has violations against the BMAD TEA Definition of Done.
Fix ALL violations while preserving the test's functionality.

ORIGINAL SCRIPT:
{original_script}

VIOLATIONS:
{violations_json}

BMAD TEA 8 MANDATORY QUALITY CRITERIA:
1. TQ-001: No hard waits — replace sleep/waitForTimeout with explicit waits (expect, waitForSelector)
2. TQ-002: No conditionals in test flow — use deterministic test paths, not if/else
3. TQ-003: Concise (<300 lines) — split large tests into focused scenarios
4. TQ-004: Fast (<90s) — optimize slow operations, use API shortcuts
5. TQ-005: Self-cleaning — add afterEach/teardown that cleans up via API calls
6. TQ-006: Visible assertions — put expect() in test body, not hidden in helpers
7. TQ-007: Unique data — use faker/uuid for test data, not hardcoded IDs
8. TQ-008: Parallel-safe — no shared mutable state, use test-scoped data

Output: The fixed test script ONLY. No prose or explanations.
"""

K6_GENERATION = """\
You are a k6 performance test engineer.

Generate a k6 performance test for this scenario:

{performance_requirement}
SLA threshold: {sla_threshold}

[F3-6] PARSED ENDPOINT THRESHOLDS (extracted deterministically from acceptance criteria — USE EXACTLY):
{endpoint_thresholds_block}

[R85.0 — REQUIRED IMPORTS, HARD CONSTRAINT — READ THIS FIRST]

Every k6 script you generate MUST begin with these EXACT import statements,
in this order, at column 0 of the file (NO indentation, NO leading whitespace,
NOT inside any function or block). The script's FIRST LINE must be the first
`import` below.

Copy-paste-ready template (use this as your starting point — do not modify
the import lines beyond adding/removing the optional ones):

```js
import http from 'k6/http';
import {{ check, sleep }} from 'k6';
import {{ Trend, Counter, Rate }} from 'k6/metrics';  // omit if unused
import {{ SharedArray }} from 'k6/data';              // omit if unused
```

[NEGATIVE EXAMPLE — DO NOT EMIT EITHER OF THESE PATTERNS]

WRONG (bare object literal — NOT a valid import; produces ReferenceError):
```js
{{ check, sleep, Trend, Counter, Rate }}      // ← BAD: no `import` keyword
import http from 'k6/http';                   //   the bare object above
...                                            //   makes the whole file invalid
check(http.get(URL), {{}});                    //   `check is not defined` at runtime
```

WRONG (destructured assignment without `import` or `require`):
```js
const {{ check, sleep }} = 'k6';              // ← BAD: not an import
import http from 'k6/http';
```

WRONG (single combined import — k6 modules are separate; you cannot
import `http` + `check` from the same module):
```js
import {{ http, check, sleep }} from 'k6/http';  // ← BAD: http is k6/http default; check is k6 named
```

RIGHT (the ONLY acceptable form — one import statement per source module):
```js
import http from 'k6/http';
import {{ check, sleep }} from 'k6';
import {{ Trend, Counter, Rate }} from 'k6/metrics';
```

[IMPORT TABLE — which symbol comes from which module]
- `http`                                → `import http from 'k6/http';`  (default export)
- `check`, `sleep`, `group`, `fail`     → `import {{ check, sleep }} from 'k6';`
- `Trend`, `Counter`, `Rate`, `Gauge`   → `import {{ Trend, Counter }} from 'k6/metrics';`
- `SharedArray`                         → `import {{ SharedArray }} from 'k6/data';`
- `b64encode`, `b64decode`              → `import {{ b64encode }} from 'k6/encoding';`

NEVER:
- emit a bare `{{ identifier1, identifier2 }}` line without `import` or `const`
- omit `import http from 'k6/http'` if the script calls `http.get()`/`http.post()`
- combine imports from different modules into one statement

Requirements:
- Multiple executor scenarios covering: ramp-up, sustained load, and spike tests
- Custom metrics for business KPIs (e.g., payment_success_rate, login_duration)
- Idempotency key in headers (prevent double-charge in load test)
- setup() for test data seeding
- teardown() for cleanup
- thresholds matching acceptance criteria EXACTLY
- Meaningful check names (for k6 Cloud dashboard)
- MUST define `export default function () {{ ... }}` whenever `scenarios` is set
  in `options`. Without it k6 fails with "function 'default' not defined".
  Alternative: add `exec: 'fnName'` to every scenario block pointing at a named
  exported function — but the default-function form is simpler and required by
  the example below.

AUTHENTICATION:
- Read token from environment: const authToken = __ENV.AUTH_TOKEN || '';
- Add to ALL authenticated requests: params = {{ headers: {{ 'Authorization': `Bearer ${{authToken}}` }} }}
- If authToken is empty the API is unauthenticated — omit the header gracefully.

[STRING SAFETY — CRITICAL]
Every JS string value ENDS at its closing quote. If the AC contains a question
or phrase, put the WHOLE phrase between the quotes — NEVER continue English
words after the closing quote, those become bare JS tokens and break parsing.
WRONG:  query: 'Why did sales drop?' when they rose,
RIGHT:  query: 'Why did sales drop when they rose?',
RIGHT:  query: "Why did sales drop?",  // double-quoted form is fine too
If the AC text contains a single quote (e.g. `we'll`, `won't`), use double-
quoted JS strings or escape the apostrophe with a backslash.

[ALL EXPORTS MUST LIVE AT MODULE TOP LEVEL — NEVER NESTED]
k6 (goja) rejects `export` keywords anywhere except module top level (column 0)
with "export only allowed in global scope". Every `export function`,
`export const`, `export default function` MUST start at column 0 — NEVER
inside another function body or block.
WRONG:                                  RIGHT:
  export default function () {{          export const options = {{ … }};
    export const options = {{ … }};      export function setup() {{ … }}
    export function setup() {{ … }}      export function teardown(_d) {{ … }}
  }}                                     export default function () {{ … }}

[SETUP / TEARDOWN — EXPORT-ONLY FORM]
k6 invokes setup() and teardown() by name as named exports. Bare top-level
`setup(() => {{...}})` calls are silent no-ops — k6 ignores them and the data
returned never reaches the default function.
WRONG:  setup(() => {{ /* seed data */ }});
RIGHT:  export function setup() {{ /* seed data */; return {{}}; }}
WRONG:  teardown((_data) => {{ /* cleanup */ }});
RIGHT:  export function teardown(_data) {{ /* cleanup */ }}

CRITICAL — SCENARIO/OPTIONS CONFLICT:
When `scenarios:` is defined inside `options`, NEVER emit `duration`, `vus`, `stages`,
or `iterations` at the top level of `options`. Those fields MUST be inside each
scenario block only. k6 rejects the combination with "unknown field" errors.
WRONG:  export const options = {{ duration: '60s', scenarios: {{ ... }} }}
RIGHT:  export const options = {{ scenarios: {{ test: {{ executor: 'constant-vus', duration: '2m', vus: 10 }} }} }}

VALID K6 EXECUTOR NAMES (kebab-case ONLY — the ONLY valid names):
- ramping-vus       (ramp VUs up/down in stages)
- constant-vus      (hold steady VU count)
- per-vu-iterations (each VU runs N iterations)
- shared-iterations (all VUs share N iterations total)

CRITICAL: k6 executor names MUST be kebab-case. NEVER use camelCase.
WRONG: executor: 'rampingVUs'     // Invalid — k6 rejects this
RIGHT: executor: 'ramping-vus'    // Valid

EXAMPLE SCENARIO STRUCTURE:
// F12-7: source uses double-braces which render as single braces via Python
// .format() — k6 / JS require single braces. Quadruple-brace source was a
// template-leak bug.
export const options = {{
  scenarios: {{
    ramp_up: {{
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        {{ duration: '30s', target: 50 }},
        {{ duration: '1m', target: 50 }},
        {{ duration: '10s', target: 0 }},
      ],
      gracefulRampDown: '10s',
    }},
    sustained: {{
      executor: 'constant-vus',
      vus: 25,
      duration: '2m',
      startTime: '1m40s',
    }},
  }},
  thresholds: {{
    // [F3] Per-endpoint thresholds — one threshold per endpoint, each tied to AC text.
    // NEVER use a single global p95 — it hides per-endpoint regressions.
    // F12-7: example below uses SINGLE braces in the rendered prompt — that
    // is the only valid k6 threshold-key syntax. Earlier source used too
    // many braces and the leak silently no-op'd every per-endpoint SLA.
    'http_req_duration{{endpoint:login}}':    ['p(95)<500'],
    'http_req_duration{{endpoint:checkout}}': ['p(95)<3000'],
    'http_req_failed': ['rate<0.01'],
  }},
}};

// REQUIRED when `scenarios` is set: every script must export a default function
// (or set `exec: 'fnName'` on every scenario block — choose one). Without this k6
// fails to start with "executor <name>: function 'default' not defined".
export default function () {{
  const params = {{ headers: {{ 'Authorization': `Bearer ${{__ENV.AUTH_TOKEN || ''}}` }} }};
  const res = http.get(`${{__ENV.BASE_URL}}/`, {{ ...params, tags: {{ endpoint: 'health' }} }});
  check(res, {{ 'status is 2xx': (r) => r.status >= 200 && r.status < 300 }});
}}

[F3] REQUIRED — Per-endpoint thresholds:
- For EACH endpoint hit by the script, define ONE threshold tied to a SPECIFIC AC text snippet
- Use k6 tagging on each request — exactly the form shown in the example above
- The threshold key MUST match the tag: same single-brace form as the example
- Cite the AC text in a comment above each threshold
- Endpoint TAG VALUES must match the regex [a-zA-Z0-9_]+ — k6 rejects dashes,
  dots, colons, slashes inside the {{endpoint:...}} selector.
- Sanitize endpoint names: replace any non-word char with underscore.
  EXAMPLES:
    /api/organization.create   →  endpoint:organization_create
    /api/generate-schema       →  endpoint:generate_schema
    /v1/credit-balance         →  endpoint:credit_balance

[F3] DURATION BUDGET — HARD LIMIT 60s TOTAL
The sum of every `duration:` literal across ALL scenarios MUST be ≤ 60 seconds.
The runtime kills scripts at 200s. Scripts longer than the budget produce
TIMEOUTS, not performance signal — the gate sees nothing. The validator will
REJECT scripts that exceed 60s cumulative duration.
- Ramp-up stage: 5s
- Sustained stage: 10s (P0 may use up to 20s)
- Spike stage: 5s
- Cool-down: 5s
Per-scenario max: 25s. Total cumulative across scenarios: ≤ 60s.
WRONG (timeouts): stages of 30s + 1m + 10s = 100s
RIGHT:           stages of 5s + 10s + 5s + 5s = 25s

Output: Complete k6 JavaScript file. Return ONLY the raw code — do NOT wrap in markdown code fences (no ```). No introductory text, no explanations.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 4B · DATA FACTORY GENERATION
# ══════════════════════════════════════════════════════════════════════════════

DATA_FACTORY_GENERATION = """\
You are a BMAD TEA Test Data Engineer.

Generate a companion data factory file for this test:

TEST FILE:
{test_script}

ENTITY CONTEXT:
{entity_context}

FACTORY REQUIREMENTS:
- Use faker library for realistic data generation
- Override pattern: factory functions accept partial overrides
- createViaAPI() — create entity via API endpoint, return created object
- deleteViaAPI() — cleanup entity via API endpoint
- Each factory is a standalone function (no class inheritance)
- Support bulk creation: createMany(count)
- Include TypeScript types/interfaces for factory output

EXAMPLE PATTERN:
```typescript
import {{ faker }} from '@faker-js/faker'
import {{ request }} from '@playwright/test'

interface UserData {{
  email: string
  name: string
  password: string
}}

export function buildUser(overrides: Partial<UserData> = {{}}): UserData {{
  return {{
    email: faker.internet.email(),
    name: faker.person.fullName(),
    password: faker.internet.password({{ length: 12 }}),
    ...overrides,
  }}
}}

export async function createUserViaAPI(
  apiContext: APIRequestContext,
  overrides: Partial<UserData> = {{}},
): Promise<UserData & {{ id: string }}> {{
  const data = buildUser(overrides)
  const res = await apiContext.post('/api/users', {{ data }})
  return res.json()
}}

export async function deleteUserViaAPI(
  apiContext: APIRequestContext,
  userId: string,
): Promise<void> {{
  await apiContext.delete(`/api/users/${{userId}}`)
}}
```

Output: Complete factory TypeScript file. No prose. Return ONLY the raw code — do NOT wrap in markdown code fences (no ```). No introductory text, no explanations.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 4C · FRAMEWORK SCAFFOLD
# ══════════════════════════════════════════════════════════════════════════════

FRAMEWORK_SCAFFOLD = """\
You are a BMAD TEA Framework Setup Agent.

Generate a test framework scaffold for this project:

PROJECT:
- Name: {project_name}
- Type: {stack_type}
- Is Greenfield: {is_greenfield}
- Engagement Model: {engagement_model}

SCAFFOLD REQUIREMENTS:
- Directory structure following BMAD TEA conventions
- Configuration files (playwright.config.ts, tsconfig.json, package.json)
- CI/CD pipeline configuration for {ci_provider}
- Sample smoke test demonstrating patterns
- README with setup instructions
- .env.example with required environment variables
- Test utilities (fixtures, helpers, factories directories)

DIRECTORY STRUCTURE:
```
tests/
  e2e/           # End-to-end UI tests
  api/           # API integration tests
  performance/   # k6 performance tests
  security/      # OWASP ZAP security tests
  fixtures/      # Shared test fixtures
  factories/     # Data factories (faker-based)
  helpers/       # Utility functions
  pages/         # Page Object helpers (flat, no inheritance)
playwright.config.ts
k6.config.js
```

Output: JSON with file paths as keys and file contents as values.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 6 · DEFECT ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

ROOT_CAUSE_ANALYSIS = """\
You are an expert software debugger performing root cause analysis.

Failed test: {test_id} — {test_title}
Type: {test_type} | Priority: {priority}

Failure evidence:
Error: {error_message}
Stack trace:
{stack_trace}

Recent changes:
{changed_files}

Diff (if available):
{diff_snippet}

ANALYSIS REQUIRED:
1. Exact root cause (file:line if possible)
2. Classification: BUG | ENV | TEST | REGRESSION | PERFORMANCE | SECURITY
3. Confidence score (0.0–1.0)
4. Impact radius — what else could be broken?
5. Specific fix with code snippet
6. Regression tests to prevent recurrence
7. Estimated fix effort: minutes | hours | days

Do not guess — if insufficient information, state what is needed.
Output ONLY JSON.
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 9 · QUALITY GATE
# ══════════════════════════════════════════════════════════════════════════════

QUALITY_GATE_DECISION = """\
You are the ARTA Quality Gate Authority — a virtual Principal Test Architect.

Evaluate this evidence package and make a PASS/CONCERNS/FAIL/WAIVED recommendation.

EVIDENCE:
Coverage Report: {coverage_json}
Execution Results: {execution_summary}
Open Defects: {defects_summary}
NFR Results: {nfr_results}

QUALITY GATE THRESHOLDS:
{thresholds_json}

DECISION CRITERIA (evidence-based only):
1. P0 requirements: must have 100% test coverage and 100% pass rate
2. Zero open P0 defects allowed
3. Performance p95 must be within SLA
4. No critical security findings
5. Overall coverage ≥ configured threshold
6. Any risk_score = 9 (AUTO_FAIL) requirement with incomplete coverage → FAIL
7. Risk scores 6-8 with warnings but mitigation owners assigned → CONCERNS (not FAIL)

FOUR POSSIBLE OUTCOMES:
- PASS     — all checks clear, no blocking issues
- CONCERNS — risk 6-8 warnings exist with assigned mitigation owners
- FAIL     — any score=9 uncovered, or critical check failed
- WAIVED   — authorized exception with rationale + expiry date (requires active waiver)

IMPORTANT:
- Base decision ONLY on evidence — never on opinion or risk tolerance
- List every blocking check with actual vs expected values
- Distinguish FAIL (release-preventing) from CONCERNS (advisory with owners)

Output JSON:
{{
  "decision": "PASS|CONCERNS|FAIL|WAIVED",
  "confidence": 0.0–1.0,
  "summary": "...",
  "blocking_checks": [...],
  "warnings": [...],
  "passed_checks": [...],
  "release_notes": "..."
}}
"""

# ══════════════════════════════════════════════════════════════════════════════
# TEA LAYER 6 · SELF-HEALING — LLM-GUIDED FIX GENERATION
# ══════════════════════════════════════════════════════════════════════════════

SELF_HEAL_FIX = """\
You are an expert Playwright test repair engineer. Produce the minimal fix that makes the failing test PASS.

TEST ID: {test_id}
TEST TITLE: {test_title}

ERROR MESSAGE:
{error_message}

CURRENT TEST FILE:
```typescript
{test_code}
```
{retry_context}
━━━ ERROR PATTERN → CORRECT FIX (apply the matching rule FIRST) ━━━

| If error contains...                          | Correct fix                                          |
|-----------------------------------------------|------------------------------------------------------|
| "intercept", "portal", "pointer-events"       | Add {{ force: true }} to the .click() — e.g. `await btn.click({{ force: true }})` |
| "Timeout", "waiting for locator", "not found" | Add waitForSelector before action, or increase timeout |
| "strict mode", "resolved to N elements"       | Add .first() or .nth(0) to narrow the locator       |
| "detached", "not attached to DOM"             | Add page.waitForTimeout(500) before the action       |
| "Navigation", "net::ERR_", "connection"       | Wrap in try/catch or fix the base URL               |

EXAMPLE — portal interception (WRONG vs CORRECT):
  ERROR: "locator.click: Intercept error: <nextjs-portal> is intercepting pointer events"
  WRONG FIX:  const btn = page.locator('button[data-id="create"]')   ← changing selector does NOT help
  CORRECT FIX: await createBtn.click({{ force: true }})               ← add force to the click call

━━━ CRITICAL RULES ━━━
- Return ONLY valid JSON — no prose, no markdown fences, no trailing commas
- `current_code` MUST be an exact verbatim substring from the test file above — copy it character-for-character
- For click interception errors: fix the .click() call, NEVER change the selector
- Do NOT rewrite selectors unless the error explicitly says the element was not found
- Do NOT change test assertions or test logic

JSON schema:
{{
  "root_cause": "<one sentence>",
  "strategy": "selector_update" | "wait_selector" | "threshold_adjustment",
  "confidence": <0.0-1.0>,
  "reasoning": "<why this specific change will make the test pass>",
  "current_code": "<exact verbatim substring to replace>",
  "proposed_code": "<replacement>",
  "diff": "<unified diff>"
}}
"""


# R126.A — compressed Playwright gen template for small Ollama models.
# The full PLAYWRIGHT_GENERATION above is ~14.5K chars (3.6K tokens) — fine
# for Claude's 200K context but consumes 23% of qwen-pro 32B's 16K-token
# budget. Combined with R47.1b/R98.3/R104.B/MULTI_ROLE blocks, total
# leaving too little for output → truncation → unbalanced braces).
#
# R126.A activates this compressed variant when provider==ollama. ~4K chars
# instead of 14.5K. Keeps ONLY what Ollama needs to produce a valid spec:
#   - Required imports (deterministic shape)
#   - The 3 forbidden-pattern callouts (request misuse, hook-use misuse,
#     duplicate imports — Ollama's chronic failure modes per run d51ce37b)
#   - Selector-hierarchy rule (testid > role+name > text)
#   - AC traceability comment requirement
#   - Output format: TypeScript ONLY, no markdown, no prose
# Drops: BMAD narrative, multi-example block, RBAC template (added
# separately via MULTI_ROLE when roles configured), expanded selector
# strategies, error-handling philosophy.
PLAYWRIGHT_GENERATION_OLLAMA = """\
[OUTPUT SHAPE — read first]
Mode: FULL_FILE_COMPACT
Output: one complete Playwright spec file (TypeScript).
Wrap with `import` / `test.describe(...)` / `test(...)`: YES
Output starts with: `import` keyword (first line is an import statement)
Output ends with: closing `}});` of the last test() block AND closing `}});` of test.describe
DO NOT emit: bare `test()` bodies without describe/imports; markdown fences (```);
  prose explanations; JSON; partial scaffolds.

Generate a Playwright TypeScript test spec for the Gherkin scenarios below.

REQUIRED IMPORTS (use exactly these, NEVER add aliases):
  import {{ test, expect, Page }} from '@playwright/test';
  import {{ waitForSPAReady, skipIfAuthStale }} from '../common/sub_flows';

OUTPUT FORMAT:
- TypeScript code ONLY, no markdown fences, no prose
- One `test.describe(...)` block wrapping all tests
- One `test('<ac_id>: <gherkin scenario title>', async ({{ page }}) => {{ ... }})` block per scenario
- Each test must have a `// AC: <ac_id>` comment on the line BEFORE `test(...)`
- R284: `<ac_id>` is the acceptance-criterion id EXACTLY as it appears in the
  Gherkin above (e.g. `AC-OR-022-01`). Copy it verbatim. Do NOT renumber it,
  do NOT invent `AC-1`/`AC-001` — those examples used to appear here and the
  model copied the SHAPE, producing ids no requirement has, which fails
  traceability. If a scenario shows no id, use its exact title.

SELECTOR RULES (use ONLY catalog entries from HARD CONSTRAINT above; NEVER invent):
  Preferred: page.getByRole('button', {{ name: 'X' }}) where X is from catalog
  Fallback:  page.getByText('exact string')
  FORBIDDEN: CSS selectors, XPath, .nth(), data-testid not in catalog, hallucinated names

API REQUEST RULES (use ONLY paths from R98.3 HARD CONSTRAINT above; NEVER invent):
  page.request.get/post(`${{API_BASE_URL}}/api/v1/<real-path>`, {{ ... }})

FORBIDDEN PATTERNS (these break at runtime — DO NOT emit):
  1. Bare `request.X(url)` factory call → TypeError. Use `page.request.X(url)` OR destructure `{{ request }}` from fixture.
  2. `await use()` inside beforeEach/afterEach → TypeError. Use plain async ({{page}}) => {{}} signature.
  3. Duplicate imports of the same name from '@playwright/test'.
  4. Unknown fixtures in test signature: ONLY {{ page, context, request, browser, browserName }} are valid PW built-ins.

[R127.D.6.B — DO-NOT-CALL `authenticate()` HARD CONSTRAINT]
There is NO `authenticate()` function in the test runtime. Auth state is
preset via storageState (loaded by auth-setup.ts). DO NOT emit
`await authenticate(page)` — the test will crash with `ReferenceError:
authenticate is not defined`. Verify auth via R112.E `skipIfAuthStale`.

[R114.C — SPA HYDRATION HARD CONSTRAINT]
After EVERY `await page.goto(...)` on an authenticated route:
  await waitForSPAReady(page);
This prevents `getByRole(...).toBeVisible()` from timing out during
React hydration. Import already in REQUIRED IMPORTS above.

[R112.E — AUTH-STATE VERIFICATION HARD CONSTRAINT]
At the start of EVERY test() block that needs authenticated state:
  await skipIfAuthStale(page);
This fails-fast if storage state is stale (e.g., session token expired).
Import already in REQUIRED IMPORTS above.

[R137.A — ANALYTICS GHERKIN TRANSLATION HARD CONSTRAINT]
When a Gherkin step says `Then <field>.<sub> should be '<value>'`
(e.g., `Then insight.metric should be 'sales'`), the SUT's response
contains `insight.metric='sales'` AND the dashboard UI renders the
**VALUE** (`'sales'`), not the field name (`'insight.metric'`).

  WRONG — the literal field name will NEVER appear in the visible UI:
    await expect(page.getByText('insight.metric')).toBeVisible();
    await expect(page.getByText('insight.org_id')).toBeVisible();

  RIGHT (UI assertion — assert the rendered VALUE):
    await expect(page.getByText('sales')).toBeVisible({{ timeout: 10000 }});

  RIGHT (API assertion — when value is hard to find on screen):
    const resp = await page.waitForResponse(r => r.url().includes('/pipeline'));
    const json = await resp.json();
    expect(json.insight.metric).toEqual('sales');

This applies to ALL analytics-shape fields: `insight.*`, `narrative.*`,
`query.*`, `result.*`, `magnitude.*`. Treat the Gherkin field paths as
JSON-property paths in the SUT response — NOT as visible UI strings.

EACH TEST MUST INCLUDE:
- At least 1 `await page.goto(...)` or navigation step
- `await waitForSPAReady(page);` after each goto on authenticated routes
- `await skipIfAuthStale(page);` for tests needing auth
- At least 2 `expect(...)` assertions
- AC comment line directly above `test()`

Gherkin scenario(s) to translate:
{gherkin_scenario}
"""


# R126.A — compressed quality rules for small Ollama models.
# The full TEST_GENERATION_QUALITY_RULES above is ~2K chars; for Ollama
# we compress to ~500 chars covering only the rules that produce a
# distinct runtime failure mode if violated.
TEST_GENERATION_QUALITY_RULES_COMPACT = """\
## Mandatory rules:
- NO sleep() or waitForTimeout(); use expect(...).toBeVisible() / waitForResponse() / waitForLoadState()
- NO if/else branching inside test() bodies — one deterministic path per test
- Each test() MUST have ≥2 expect() assertions
- crypto.randomUUID()/Date.now() ONLY for entities the test CREATES; for
  READ/UPDATE/DELETE use a real id or {{entity_id}} (random id on GET = 404)
- Cleanup: use beforeEach/afterEach with API calls (NEVER UI) to seed/teardown
- Each spec file under 300 lines

## Selector hierarchy (strict):
1. data-testid (preferred when in catalog)
2. getByRole(role, {{ name }}) from catalog
3. getByText('exact')
FORBIDDEN: .nth(), XPath, CSS class selectors
"""
