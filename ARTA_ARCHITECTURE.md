# ARTA — AI Requirements & Test Architect
## Complete System Architecture & Design Specification

> **ARTA** operationalizes the **BMAD TEA (Test Engineering Architecture)** methodology through a multi-agent AI platform that autonomously performs all functions of a Principal Test Architect inside the software development lifecycle.

---

## 1. Product Vision

### Problems with Current QA Automation

| Problem | Impact |
|---|---|
| Tests written after code ships | Defects found too late, expensive to fix |
| Manual test design bottleneck | QA teams gatekeep delivery speed |
| Brittle selectors & hardcoded scripts | 30-50% of automation suites break on UI changes |
| No traceability | Cannot prove coverage; compliance failures |
| Risk-blind prioritization | Low-risk tests run first, critical areas missed |
| Siloed QA teams | Dev–QA handoff friction, no shared ownership |
| Tool fragmentation | 5+ tools, no unified view, no intelligence |

### Why Traditional Tools Fail

- **Selenium/Playwright alone**: execution tools, not architects
- **TestRail/Xray**: manual record-keeping, no AI reasoning
- **Coverage tools**: code coverage ≠ requirement coverage
- **AI test generators (Copilot, etc.)**: generate code, not strategy

### How AI + TEA Transforms Quality Engineering

ARTA embeds a **virtual Test Architect** into every sprint who:
1. Reads requirements before a single line of code is written
2. Designs acceptance criteria and test strategy proactively
3. Generates failing tests first (true ATDD)
4. Continuously monitors risk, coverage, and execution health
5. Enforces evidence-based quality gates autonomously

---

## 2. AI Test Architect Agent Concept

ARTA acts simultaneously as four roles:

```
┌─────────────────────────────────────────────────────────────┐
│                    ARTA Agent Roles                         │
│                                                             │
│  Test Architect     →  Strategy, risk, coverage design      │
│  QA Lead            →  Sprint governance, quality gates     │
│  Automation Eng.    →  Script generation, self-healing      │
│  Quality Authority  →  Release decisioning, compliance      │
└─────────────────────────────────────────────────────────────┘
```

### Manual Overhead Reduction

| Activity | Manual Effort | With ARTA |
|---|---|---|
| Test strategy per feature | 4h / feature | 30s |
| Acceptance criteria review | 2h / story | Instant |
| Test case authoring | 8h / module | 2-5 min |
| Traceability mapping | 1 day / sprint | Continuous |
| Root cause analysis | 30-60 min / defect | < 60s |
| Quality gate assessment | Manual review | Automated |

---

## 3. Internal TEA Architecture Implementation

ARTA implements all nine TEA layers internally:

```
┌─────────────────────────────────────────────────────────────────┐
│  TEA LAYER 1 · Test Strategy Architecture                       │
│  → Analyzes product vision, tech stack, team maturity           │
│  → Produces: Testing Pyramid, Coverage Matrix, Tool Chain       │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 2 · Risk-Based Test Design                           │
│  → Risk = Impact(1-3) × Probability(1-3) → score 1-9           │
│  → P0 (≥8) → P1 (6-7) → P2 (4-5) → P3 (≤3)                   │
│  → Actions: DOCUMENT / MONITOR / MITIGATE / AUTO_FAIL           │
│  → Produces: Prioritized Test Schedule, Risk Heatmap            │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 3 · Acceptance Test Definition                       │
│  → Parses requirements → Generates Gherkin scenarios            │
│  → Enforces: Given/When/Then with concrete data                 │
│  → Produces: Feature files, AC Coverage Matrix                  │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 4 · Test Automation Strategy                         │
│  → Selects tool per test type automatically                     │
│  → Playwright (UI) · Newman (API) · k6 (Perf) · OWASP (Sec)   │
│  → Produces: Automation blueprints, CI pipeline config          │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 5 · Test Execution                                   │
│  → Parallel execution orchestration                             │
│  → Environment management, test data provisioning               │
│  → Produces: Real-time execution feed, failure artifacts        │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 6 · Evidence Collection                              │
│  → Screenshots, HAR files, API logs, performance traces         │
│  → Immutable audit trail for compliance                         │
│  → Produces: Evidence packages per test run                     │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 7 · Traceability & Coverage Analysis                 │
│  → Neo4j graph: Requirement→AC→Scenario→TC→Result→Defect        │
│  → 3-state coverage per AC: FULL / PARTIAL / NONE               │
│  → NONE on P0/P1 AC triggers gate FAIL                          │
│  → Auto-detects: gaps, redundancy, orphaned tests               │
│  → Produces: Coverage report, gap analysis                      │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 8 · Non-Functional Validation (4-Category Rubrics)   │
│  → Security: auth bypass, OWASP top-10, token expiry ≤15min     │
│  → Performance: p95 <500ms, p99 <1s, error rate <1%             │
│  → Reliability: health checks, retry logic, circuit breakers    │
│  → Maintainability: coverage ≥80%, duplication <5%, 0 crit vulns│
│  → Produces: NFR scorecard, SLA compliance report               │
├─────────────────────────────────────────────────────────────────┤
│  TEA LAYER 9 · Quality Gate Decisioning (4-Outcome)             │
│  → PASS: all checks clear                                       │
│  → CONCERNS: risk 6-8 with assigned mitigation owners           │
│  → FAIL: score=9 or uncovered P0/P1 AC                         │
│  → WAIVED: authorized exception + rationale + expiry (admin)    │
│  → Produces: Release recommendation + audit log                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Multi-Agent System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        ARTA Multi-Agent System                              │
│                                                                             │
│  ┌──────────────────────┐      ┌──────────────────────────────────────┐    │
│  │  ORCHESTRATOR AGENT  │      │         AGENT MESSAGE BUS             │    │
│  │  (Master Coordinator)│◄────►│   (Redis Pub/Sub + Event Sourcing)   │    │
│  └──────────────────────┘      └──────────────────────────────────────┘    │
│           │                                                                 │
│    ┌──────┴────────────────────────────────────────────────┐               │
│    ▼                ▼               ▼                ▼      │               │
│  ┌─────────┐  ┌─────────┐  ┌─────────────┐  ┌──────────┐  │               │
│  │ Req.    │  │ Strategy│  │  ATDD       │  │ Automat. │  │               │
│  │ Intel   │  │ Arch.   │  │  Designer   │  │ Engineer │  │               │
│  │ Agent   │  │ Agent   │  │  Agent      │  │ Agent    │  │               │
│  └─────────┘  └─────────┘  └─────────────┘  └──────────┘  │               │
│    ▼                ▼               ▼                ▼      │               │
│  ┌─────────┐  ┌─────────┐  ┌─────────────┐  ┌──────────┐  │               │
│  │Execution│  │ Defect  │  │Traceability │  │  Quality │  │               │
│  │ Agent   │  │ Intel   │  │  Agent      │  │   Gate   │  │               │
│  │         │  │ Agent   │  │             │  │   Agent  │  │               │
│  └─────────┘  └─────────┘  └─────────────┘  └──────────┘  │               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Agent Responsibilities

#### 1. Requirement Intelligence Agent
```
Inputs:  PRD, User Stories, API Specs, DB Schemas
Tools:   LLM (Claude), RAG over docs, NLP parser
Outputs: Structured requirements, acceptance criteria, entity models
Triggers: New PR, story ticket created, API spec changed
```

#### 2. Test Strategy Architect Agent
```
Inputs:  Requirements, risk profile, tech stack manifest
Tools:   TEA risk calculator, coverage matrix builder
Outputs: Test strategy doc, pyramid allocation, tool assignments
Priority: Risk score = Impact(1-3) × Probability(1-3) → 1-9
```

#### 3. Acceptance Test Designer Agent
```
Inputs:  Acceptance criteria, domain context
Tools:   Gherkin generator, scenario expander, BDD validator
Outputs: .feature files, edge cases, negative scenarios, security scenarios
Format:  Given/When/Then with concrete data tables
```

#### 4. Automation Engineer Agent
```
Inputs:  Gherkin scenarios, API specs, UI component inventory
Tools:   Playwright/Cypress/Newman/k6 code generators
Outputs: Ready-to-run automation scripts, page objects, fixtures
Self-healing: Monitors selector stability, auto-repairs on failure
```

#### 5. Execution Agent
```
Inputs:  Test suite, environment config, test data
Tools:   Playwright executor, k6 runner, Docker orchestrator
Outputs: Real-time execution feed, artifacts (screenshots, HAR, logs)
Parallelism: Matrix execution across browsers/environments
```

#### 6. Defect Intelligence Agent
```
Inputs:  Failure artifacts, stack traces, test history
Tools:   LLM root cause analyzer, similarity detection, code navigator
Outputs: Structured bug report, root cause, suggested fix, impact radius
Auto-files: Jira/GitHub Issues with full context
```

#### 7. Traceability Agent
```
Inputs:  Requirements, test results, defects
Tools:   Neo4j graph manager, coverage calculator
Outputs: Traceability graph, coverage gaps, orphan detection
Alerts:  Uncovered requirements, redundant tests, broken chains
```

#### 8. Quality Gate Agent
```
Inputs:  Execution results, coverage data, risk scores, NFR metrics
Rules:   Configurable thresholds per environment/risk level
Outputs: 4-outcome decision (PASS/CONCERNS/FAIL/WAIVED), evidence package
         WAIVED requires admin-only approval with rationale + expiry date
Integrations: GitHub status checks, Jenkins gate, Azure DevOps
```

---

## 5. TEA-Driven ATDD Workflow

```
  REQUIREMENT ARRIVES (GitHub Issue / Jira Story)
           │
           ▼
  ┌─────────────────────────────┐
  │  Requirement Intel Agent    │ → Extracts entities, constraints,
  │  "Parsing REQ-017..."       │   acceptance criteria from natural language
  └─────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────┐
  │  Strategy Architect Agent   │ → Risk Score: 9.4/10 (P0)
  │  "Risk Analysis..."         │   Test types: UI+API+Perf+Security
  └─────────────────────────────┘   Coverage target: 100% (P0)
           │
           ▼
  ┌─────────────────────────────┐
  │  ATDD Designer Agent        │ → Generates failing Gherkin tests FIRST
  │  "Writing scenarios..."     │   12 scenarios, 4 edge cases, 3 security
  └─────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────┐
  │  Automation Engineer Agent  │ → Playwright scripts (UI)
  │  "Generating scripts..."    │   Newman collection (API)
  └─────────────────────────────┘   k6 load script (Perf)
           │
           ▼
  ┌─────────────────────────────┐
  │  Execution Agent            │ → All tests FAIL (red phase — ATDD)
  │  "Running suite..."         │   Reports to developer: implement feature
  └─────────────────────────────┘
           │
       [Dev codes feature]
           │
           ▼
  ┌─────────────────────────────┐
  │  Execution Agent (re-run)   │ → Tests turn GREEN as feature passes
  │  "Re-running on commit..."  │   Performance: 2.1s avg (< 3s ✓)
  └─────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────┐
  │  Defect Intel Agent         │ → Analyzes remaining failures
  │  "Analyzing failures..."    │   Root cause + suggested fix + Jira ticket
  └─────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────┐
  │  Traceability Agent         │ → Updates REQ-017 coverage: 87%
  │  "Updating graph..."        │   AC-004 still uncovered → flags alert
  └─────────────────────────────┘
           │
           ▼
  ┌─────────────────────────────┐
  │  Quality Gate Agent         │ → P0 coverage < 100% → BLOCK DEPLOY
  │  "Gate Assessment..."       │   AC-004 must be covered before release
  └─────────────────────────────┘
```

---

## 6. Requirement Intelligence

### Parsing Inputs
```python
class RequirementIntelAgent:
    def parse(self, input: RequirementInput) -> StructuredRequirement:
        """
        Accepts:
          - User stories (Jira/Linear format)
          - PRD documents (PDF, Confluence, Notion)
          - OpenAPI/Swagger specs
          - Architecture Decision Records
          - Database schema files

        Uses RAG pipeline over:
          - Existing test history
          - Domain glossary
          - Previous AC patterns
        """

    def extract_acceptance_criteria(self, req) -> list[AcceptanceCriterion]:
        """
        Pattern: Given [precondition] When [action] Then [outcome]

        Also extracts:
          - Business rules (must/shall/should)
          - Data constraints (formats, ranges, uniqueness)
          - Performance expectations (within Xs)
          - Security requirements (must not allow)
          - Error scenarios (if...then error)
        """
```

### Output Data Model
```json
{
  "requirement_id": "REQ-017",
  "title": "Checkout Payment Processing",
  "priority": "P0",
  "risk_score": 9.4,
  "acceptance_criteria": [
    {
      "id": "AC-001",
      "statement": "Given valid card details, When payment submitted, Then order confirmed within 3s",
      "test_types": ["UI", "API", "Performance"],
      "data_requirements": ["valid_visa", "valid_mastercard", "amex"],
      "covered": true,
      "test_count": 3
    }
  ],
  "entities": ["Card", "Order", "Transaction", "User"],
  "constraints": ["PCI-DSS", "3DS required", "max 3 retry attempts"]
}
```

---

## 7. Risk-Based Testing

### Risk Formula (1-3 Scale)

```
Impact (1-3):      1 = Minor  |  2 = Degraded  |  3 = Critical
Probability (1-3): 1 = Unlikely  |  2 = Possible  |  3 = Likely

Risk Score = Impact × Probability → range 1-9

Action Thresholds:
  Score 1-3  → DOCUMENT   (P3)       — record and move on
  Score 4-5  → MONITOR    (P2)       — ≥ 75% coverage required
  Score 6-8  → MITIGATE   (P1 6-7, P0 8) — ≥ 90-100% coverage, assign owner
  Score 9    → AUTO_FAIL  (P0)       — 100% coverage required, blocks release

Priority Mapping:
  P0 Critical:  Risk ≥ 8   → 100% coverage required, blocks release
  P1 High:      Risk 6-7   → ≥ 90% coverage required
  P2 Medium:    Risk 4-5   → ≥ 75% coverage required
  P3 Low:       Risk ≤ 3   → ≥ 50% coverage, best effort
```

### Risk Factors Input to AI
```python
risk_factors = {
    "business_criticality": "revenue_path",    # +3.0
    "user_exposure": "all_users",              # +2.0
    "historical_defect_rate": 0.34,            # +2.0 (34% historical failure)
    "code_complexity": "high",                 # +1.5
    "external_dependencies": ["stripe", "3ds"],# +1.0
    "regulatory_requirement": "PCI-DSS",       # +2.0 (auto P0)
    "last_changed": "2d_ago",                  # +0.5 (recent change)
}
# Score: 9.4 → P0 Critical
```

---

## 8. Acceptance Test Design

### Gherkin Generation Templates

**Happy Path Template:**
```gherkin
Feature: {feature_name}
  # {requirement_id} | Risk: {risk_score} | Priority: {priority}

  Background:
    Given the system is operational
    And I am authenticated as "{user_role}"

  Scenario: {happy_path_title}
    Given {precondition_from_ac}
    And {additional_setup}
    When {primary_action}
    Then {expected_outcome}
    And {measurable_constraint}  # e.g., within 3 seconds
```

**Edge Case Template:**
```gherkin
  Scenario Outline: {boundary_title}
    Given {setup}
    When I enter "<input_value>"
    Then I should see "<expected_result>"

    Examples:
      | input_value          | expected_result        |
      | ""                   | "Field required"       |
      | "a" * 256            | "Max length exceeded"  |
      | "'; DROP TABLE--"    | "Invalid input"        |
      | "  valid  "          | "Trimmed and accepted" |
```

**Security Template:**
```gherkin
  Scenario: SQL injection attempt blocked
    Given I am on the payment form
    When I enter "'; DROP TABLE orders; --" in card number
    Then the input should be sanitized
    And no database error should occur
    And a security event should be logged
```

---

## 9. Automated Test Generation

### Tool Selection Logic
```python
def select_automation_tool(scenario: Scenario) -> AutomationPlan:
    if scenario.type == "UI" and scenario.requires_browser:
        return PlaywrightGenerator()  # Preferred for modern web

    elif scenario.type == "API":
        return NewmanGenerator()      # Postman collections + Newman runner

    elif scenario.type == "Performance":
        return K6Generator()          # k6 for load/stress/soak

    elif scenario.type == "Security":
        return ZAPGenerator()         # OWASP ZAP active scan scripts

    elif scenario.type == "Accessibility":
        return AxeGenerator()         # axe-core integration
```

### k6 Performance Script (Auto-generated)
```javascript
// ARTA Auto-generated · TC-PERF-001 · REQ-017 AC-001
// Risk: P0 · Threshold: p95 < 3000ms
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '1m', target: 100 },   // Ramp up
    { duration: '3m', target: 500 },   // Sustained load
    { duration: '1m', target: 1000 },  // Spike
    { duration: '1m', target: 0 },     // Ramp down
  ],
  thresholds: {
    'http_req_duration': ['p95<3000'], // TEA NFR threshold
    'http_req_failed': ['rate<0.01'],  // 99% success rate
    'checks': ['rate>0.99'],
  },
};

export default function () {
  const payload = JSON.stringify({
    card: '4111111111111111',
    expiry: '12/28',
    cvv: '123',
    amount: 9999,
  });

  const res = http.post(`${__ENV.BASE_URL}/api/checkout/payment`, payload, {
    headers: { 'Content-Type': 'application/json' },
  });

  check(res, {
    'status 200': (r) => r.status === 200,
    'order confirmed': (r) => r.json('status') === 'confirmed',
    'response time < 3s': (r) => r.timings.duration < 3000,
    'no double charge': (r) => r.json('transaction_count') === 1,
  });

  sleep(1);
}
```

---

## 10. Self-Healing Automation

```python
class SelfHealingAgent:
    async def on_selector_failure(self, failed_test: TestResult) -> HealingAction:
        """
        1. Capture DOM snapshot at time of failure
        2. Compare with last-passing DOM snapshot
        3. Use LLM to identify equivalent selector
        4. Validate new selector in staging environment
        5. Route based on config flag:
             require_human_approval=True  → push to approval queue (PENDING_APPROVALS)
             require_human_approval=False → issue PR with selector update directly
        """

    async def on_api_schema_change(self, diff: APISchemaDiff) -> HealingAction:
        """
        1. Detect field rename/removal/type change via spec diff
        2. Update affected Postman collections automatically
        3. Re-run impacted tests to validate
        4. Notify team if semantic change detected (not just rename)
        """

    async def on_env_instability(self, flaky_tests: list[TestResult]):
        """
        1. Detect flakiness pattern (pass/fail ratio over 10 runs)
        2. Increase retry count for known-flaky infrastructure tests
        3. Quarantine repeatedly-flaky tests with alert
        4. Root cause analysis: external dependency or test design?
        """

    async def _queue_for_approval(self, action: HealingAction) -> str:
        """
        Push proposal to in-memory approval queue (Redis in production).
        Returns proposal_id for tracking.
        Proposal carries: test_id, strategy, confidence, diff_preview,
        current_selector, proposed_selector, ai_reasoning, status=pending.
        """
```

### Approval Queue Flow

```
SelfHealingAgent detects selector drift
          │
          ▼
   confidence >= min_confidence_for_queue (0.65)?
          │ Yes
          ▼
   require_human_approval == true?
    ├── Yes → _queue_for_approval() → PENDING_APPROVALS dict
    │          Engineer reviews in Defect Intel → Heal Approval Modal
    │          POST /api/healing/{id}/approve → GitHub PR raised
    │          POST /api/healing/{id}/edit    → custom selector → PR
    │          POST /api/healing/{id}/reject  → flagged as known-broken
    └── No  → _create_healing_pr() → GitHub PR raised immediately
```

---

## 11. Traceability Graph (Neo4j)

### Graph Schema
```cypher
// Nodes
(:Requirement {id, title, priority, risk_score, sprint})
(:AcceptanceCriteria {id, statement, covered: bool})
(:TestScenario {id, title, type, gherkin})
(:TestCase {id, title, automation_type, script_path})
(:ExecutionResult {id, status, duration, timestamp, build_id})
(:Defect {id, title, severity, status, root_cause})

// Relationships
(req)-[:HAS_AC]->(ac)
(ac)-[:DEFINES]->(scenario)
(scenario)-[:IMPLEMENTED_BY]->(tc)
(tc)-[:PRODUCED]->(result)
(result)-[:TRIGGERED]->(defect)
(defect)-[:AFFECTS]->(req)
(tc)-[:COVERS]->(ac)

// Coverage Query
MATCH (r:Requirement)-[:HAS_AC]->(ac)
OPTIONAL MATCH (ac)<-[:COVERS]-(tc)-[:PRODUCED]->(res {status:'PASS'})
RETURN r.id,
       count(distinct ac) as total_ac,
       count(distinct tc) as covered_ac,
       toFloat(count(distinct tc)) / count(distinct ac) * 100 as coverage_pct
```

### Gap Detection Query
```cypher
// Find uncovered requirements
MATCH (r:Requirement)-[:HAS_AC]->(ac)
WHERE NOT (ac)<-[:COVERS]-(:TestCase)
RETURN r.id, r.title, r.priority, collect(ac.id) as uncovered_ac
ORDER BY r.priority ASC
```

---

## 12. DevSecOps Integration

### CI/CD Pipeline YAML (GitHub Actions)
```yaml
name: ARTA Quality Pipeline

on: [push, pull_request]

jobs:
  arta-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: ARTA · Requirement Analysis
        uses: arta/requirement-agent@v2
        with:
          stories: ${{ github.event.pull_request.body }}
          api_spec: openapi.yaml

      - name: ARTA · AI Test Generation
        uses: arta/test-generator@v2
        with:
          strategy: tea-atdd
          risk_threshold: p1

      - name: ARTA · Execute UI Tests
        run: npx playwright test --reporter=arta

      - name: ARTA · Execute API Tests
        run: newman run .arta/collections/*.json

      - name: ARTA · Performance Gate (k6)
        run: k6 run .arta/performance/checkout.js
        env:
          BASE_URL: ${{ vars.STAGING_URL }}

      - name: ARTA · Security Scan (ZAP)
        uses: zaproxy/action-full-scan@v0.9

      - name: ARTA · Quality Gate Decision
        uses: arta/quality-gate@v2
        with:
          coverage_threshold: 80
          p0_coverage: 100
          p0_pass_rate: 100
          p1_pass_rate: 95
          performance_p95_ms: 3000
          # Blocks deployment if criteria not met
```

---

## 13. Developer Commands

```bash
# Slash commands via CLI / VS Code extension / chat

/generate-tests [requirement-id|story-url|feature-description]
  → Full test suite: Gherkin + automation code + test data

/generate-edge-cases [test-file|scenario-description]
  → Boundary values, null/empty, injection, overflow edge cases

/run-atdd [feature|sprint|all]
  → Execute ATDD red-green cycle, report on failures

/analyze-risk [feature|module|component]
  → TEA risk scoring, coverage gaps, recommended priority

/check-coverage [requirement-id|module]
  → Traceability report: what's covered, what's missing

/heal-tests [test-file]
  → Auto-repair broken selectors/schema references

/explain-failure [test-id|build-id]
  → Root cause analysis + suggested fix + impact radius

/generate-report [sprint|release]
  → Evidence-based quality report for stakeholders

/check-gate [environment]
  → Run quality gate check: PASS / CONCERNS / FAIL / WAIVED with reasons
```

---

## 14. Technology Stack

```
┌──────────────────────────────────────────────────────────────────┐
│                      AI LAYER                                    │
│  LLM: Claude claude-sonnet-4-6 (Primary) / claude-opus-4-6 (Strategy)  │
│  RAG: LlamaIndex + ChromaDB (vector search over docs)            │
│  Embeddings: text-embedding-3-large                              │
│  Prompt Management: LangChain + custom TEA prompt templates       │
├──────────────────────────────────────────────────────────────────┤
│                      BACKEND                                     │
│  API: FastAPI (Python 3.12) + async/await                        │
│  Agent Orchestration: CrewAI / custom event-driven graph         │
│  Message Bus: Redis Pub/Sub + Redis Streams (event sourcing)      │
│  Task Queue: Celery + Redis                                      │
│  Auth: JWT + OAuth2 (GitHub/GitLab/Azure AD SSO)                 │
├──────────────────────────────────────────────────────────────────┤
│                   TEST AUTOMATION                                │
│  UI/E2E: Playwright (primary) + Selenium (legacy support)        │
│  API: Postman/Newman + custom OpenAPI runner                     │
│  Performance: k6 (cloud + local)                                 │
│  Security: OWASP ZAP + custom injection payloads                 │
│  Accessibility: axe-core + Lighthouse                            │
├──────────────────────────────────────────────────────────────────┤
│                      DATA LAYER                                  │
│  Primary DB: PostgreSQL 16 (requirements, test metadata, runs)   │
│  Graph DB: Neo4j 5.x (traceability graph)                        │
│  Cache: Redis 7 (session, locks, live execution feed)            │
│  Vector DB: ChromaDB (document embeddings for RAG)               │
│  Object Storage: S3/MinIO (screenshots, HAR files, reports)      │
├──────────────────────────────────────────────────────────────────┤
│                      FRONTEND                                    │
│  Framework: Next.js 14 (App Router)                              │
│  UI: Tailwind CSS + Radix UI primitives                          │
│  Graph Viz: D3.js + React Force Graph (traceability)             │
│  Charts: Recharts (coverage, trend analysis)                     │
│  Real-time: Server-Sent Events / WebSocket (execution feed)      │
├──────────────────────────────────────────────────────────────────┤
│                  DEVOPS / INFRASTRUCTURE                         │
│  Containers: Docker + Kubernetes (K8s)                           │
│  CI/CD: GitHub Actions + Jenkins + Azure DevOps                  │
│  Observability: OpenTelemetry + Grafana + Prometheus             │
│  Secrets: HashiCorp Vault                                        │
│  IaC: Terraform                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 15. End-to-End Scenario: E-Commerce Checkout

### Input
```
User Story: As a customer, I want to complete checkout using my saved
payment method so that I can purchase items quickly without re-entering
card details.

Acceptance Criteria:
1. Saved cards load within 1 second of reaching checkout
2. One-click checkout completes in under 3 seconds
3. Payment is idempotent (no double charge on retry)
4. PCI-DSS: card details never stored in application logs
5. 3DS authentication triggered for transactions > £150
```

### ARTA Output (Autonomous)

**Risk Assessment:**
- Business Impact: 10/10 (revenue path)
- Failure Probability: 7/10 (external payment dependency, concurrency)
- **Risk Score: 9.4/10 → P0 Critical**

**Generated Test Suite:**

```
UI Tests (Playwright):                    12 scenarios
├─ TC-001: Saved card display < 1s        HAPPY PATH
├─ TC-002: One-click checkout success     HAPPY PATH
├─ TC-003: 3DS triggered > £150           HAPPY PATH
├─ TC-004: 3DS authentication failure     NEGATIVE
├─ TC-005: Card expired during checkout   NEGATIVE
├─ TC-006: Session timeout mid-checkout   EDGE CASE
├─ TC-007: Back button / double submit    EDGE CASE
└─ TC-008: Network drop during payment    EDGE CASE

API Tests (Newman):                       8 scenarios
├─ TC-009: POST /checkout/payment 200     HAPPY PATH
├─ TC-010: Idempotency key enforcement    CRITICAL
├─ TC-011: Duplicate request blocked      SECURITY
├─ TC-012: Card token validation          SECURITY
└─ TC-013..016: Error codes 400/402/500   NEGATIVE

Performance Tests (k6):                  3 scenarios
├─ TC-017: p95 < 3s under 500 VU          SLA
├─ TC-018: Spike test 1000 VU             STRESS
└─ TC-019: Soak test 200 VU / 30min       STABILITY

Security Tests (ZAP):                    5 scenarios
├─ TC-020: SQL injection all fields       SEC
├─ TC-021: XSS in address fields          SEC
├─ TC-022: CSRF token validation          SEC
├─ TC-023: Sensitive data in logs         COMPLIANCE
└─ TC-024: Authorization bypass           SEC

TOTAL: 28 automated tests across 4 tool chains
Coverage: 100% of P0 acceptance criteria
```

---

## 16. Data Models

### Core Schema (PostgreSQL)

```sql
-- Requirements
CREATE TABLE requirements (
    id          VARCHAR(20) PRIMARY KEY,  -- REQ-017
    title       TEXT NOT NULL,
    description TEXT,
    priority    VARCHAR(2) CHECK (priority IN ('P0','P1','P2','P3')),
    risk_score  DECIMAL(4,2),
    sprint_id   INTEGER REFERENCES sprints(id),
    source_url  TEXT,                     -- Jira/GitHub link
    status      VARCHAR(20),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Acceptance Criteria
CREATE TABLE acceptance_criteria (
    id              VARCHAR(20) PRIMARY KEY,  -- AC-001
    requirement_id  VARCHAR(20) REFERENCES requirements(id),
    statement       TEXT NOT NULL,
    given_context   TEXT,
    when_action     TEXT,
    then_outcome    TEXT,
    test_types      VARCHAR[] DEFAULT '{}',
    covered         BOOLEAN DEFAULT FALSE,
    generated_by    VARCHAR(50) DEFAULT 'arta-agent'
);

-- Test Cases
CREATE TABLE test_cases (
    id              VARCHAR(20) PRIMARY KEY,  -- TC-001
    ac_id           VARCHAR(20) REFERENCES acceptance_criteria(id),
    title           TEXT NOT NULL,
    gherkin_feature TEXT,                     -- Full .feature content
    automation_type VARCHAR(20),              -- playwright/newman/k6/zap
    script_path     TEXT,
    priority        VARCHAR(2),
    is_active       BOOLEAN DEFAULT TRUE,
    self_healed_at  TIMESTAMPTZ,
    created_by      VARCHAR(50) DEFAULT 'arta-atdd-agent'
);

-- Test Runs
CREATE TABLE test_runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    build_id    VARCHAR(100),
    trigger     VARCHAR(50),                  -- push/pr/scheduled/manual
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    status      VARCHAR(20),
    total       INTEGER,
    passed      INTEGER,
    failed      INTEGER,
    skipped     INTEGER,
    coverage_pct DECIMAL(5,2)
);

-- Execution Results
CREATE TABLE execution_results (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID REFERENCES test_runs(id),
    tc_id       VARCHAR(20) REFERENCES test_cases(id),
    status      VARCHAR(10),                  -- PASS/FAIL/SKIP/ERROR
    browser     VARCHAR(20),                  -- chromium/firefox/webkit
    duration_ms INTEGER,
    error_msg   TEXT,
    screenshot  TEXT,                         -- S3 path
    har_file    TEXT,                         -- S3 path
    evidence    JSONB,                        -- structured artifacts
    executed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Test Data Fixtures
CREATE TABLE test_data_fixtures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id     TEXT NOT NULL REFERENCES test_cases(test_id),
    fixture_id  TEXT NOT NULL,               -- e.g., "TD-001"
    description TEXT,
    columns     JSONB NOT NULL DEFAULT '[]', -- ["card_type","amount","expected"]
    rows        JSONB NOT NULL DEFAULT '[]', -- [["VISA","99.99","confirmed"],...]
    examples    TEXT,                        -- raw Gherkin Examples: block
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Test Case Version History
CREATE TABLE test_case_versions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id          TEXT NOT NULL REFERENCES test_cases(test_id),
    version          INTEGER NOT NULL,       -- 1, 2, 3, ...
    change_reason    TEXT,                   -- "Regenerated after REQ-017 updated"
    gherkin_snapshot TEXT,
    script_snapshot  TEXT,
    changed_by       TEXT DEFAULT 'arta-agent',
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(test_id, version)
);

-- Defects
CREATE TABLE defects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    result_id       UUID REFERENCES execution_results(id),
    tc_id           VARCHAR(20) REFERENCES test_cases(id),
    title           TEXT,
    severity        VARCHAR(2),
    root_cause      TEXT,                     -- LLM analysis
    suggested_fix   TEXT,                     -- LLM suggestion
    impacted_files  TEXT[],
    jira_id         VARCHAR(50),
    status          VARCHAR(20) DEFAULT 'open',
    auto_detected   BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 17. Prompt Templates

### TEA Risk Scoring Prompt
```
You are a TEA (Test Engineering Architecture) Risk Analyst.

Analyze this requirement and output a structured risk assessment.

REQUIREMENT:
{requirement_text}

CONTEXT:
- Product type: {product_type}
- User base: {user_count} users
- Regulatory requirements: {regulations}
- Historical defect rate for this module: {defect_rate}%
- Last modified: {last_modified}

Score the following dimensions (1-3 scale):

Impact (1-3):
  1 = Minor — cosmetic or low-traffic feature
  2 = Degraded — partial loss of functionality
  3 = Critical — revenue path, security, or compliance

Probability (1-3):
  1 = Unlikely — stable module, no recent changes
  2 = Possible — moderate complexity or external deps
  3 = Likely — high churn, known fragility, recent incidents

Risk Score = Impact × Probability (range 1-9)

Action thresholds:
  1-3 → DOCUMENT (P3)
  4-5 → MONITOR (P2)
  6-8 → MITIGATE (P1/P0)
  9   → AUTO_FAIL (P0)

Output JSON:
{
  "impact": <1-3>,
  "probability": <1-3>,
  "risk_score": <1-9>,
  "action": "DOCUMENT|MONITOR|MITIGATE|AUTO_FAIL",
  "priority": "P0|P1|P2|P3",
  "rationale": "<2-3 sentences>",
  "recommended_test_types": ["UI","API","Performance","Security"],
  "coverage_target_pct": <number>
}
```

### Gherkin Generation Prompt
```
You are a TEA Acceptance Test Designer. Generate comprehensive Gherkin
test scenarios from this acceptance criterion.

REQUIREMENT: {req_id} - {req_title}
ACCEPTANCE CRITERION: {ac_statement}
RISK PRIORITY: {priority}
DOMAIN CONTEXT: {domain_entities}

Generate scenarios covering:
1. Happy path (primary success flow)
2. All boundary conditions (min/max/empty/null)
3. Negative cases (invalid inputs, error states)
4. Security cases (injection, auth bypass, data exposure)
5. Performance assertion (where timing constraints exist)
6. Concurrency/idempotency (if state-mutating operation)

Rules:
- Use concrete data values (not placeholders like <valid_card>)
- Include Examples tables for Scenario Outlines
- Reference test data fixtures by name
- Add comments linking to requirement and risk score

Format: Valid Gherkin (.feature file format)
```

---

## 18. Autonomous QA Future

```
Phase 1 (Current): Assisted
  Human writes requirements → ARTA generates tests + automation
  Human reviews → ARTA executes + reports
  Quality gates: configurable thresholds

Phase 2 (2026): Collaborative
  ARTA reads Figma/design specs → generates tests before dev starts
  ARTA monitors code commits → updates tests automatically
  ARTA detects production anomalies → generates regression tests

Phase 3 (2027): Autonomous
  ARTA reads roadmap → proactively designs test architecture
  ARTA detects production incidents → self-creates diagnostic tests
  ARTA suggests feature refactoring to improve testability
  ARTA manages entire test estate: creates, retires, evolves

Phase 4 (Horizon): Self-Governing Quality
  ARTA negotiates quality contracts with product teams
  ARTA predicts defect probability before code is written
  ARTA autonomously certifies releases with full audit trail
  Quality engineering becomes a background autonomous service
```

---

## File Structure

```
 arta-platform/
├── arta-platform.html            # Standalone UI prototype (zero deps, open in browser)
├── agents/
│   ├── requirement_intel.py      # Requirement parsing + multi-format file ingestion
│   ├── strategy_architect.py     # TEA strategy + risk scoring
│   ├── atdd_designer.py          # Gherkin generation + test data fixtures + project types
│   ├── automation_engineer.py    # Code generation
│   ├── execution_agent.py        # Test runner orchestration
│   ├── defect_intel.py           # Root cause analysis
│   ├── traceability_agent.py     # Neo4j graph management
│   ├── quality_gate_agent.py     # Release decisioning (4-outcome)
│   ├── self_healing.py           # Selector repair + approval queue + GitHub PR
│   ├── test_review_agent.py     # Test quality scoring (8-criteria DoD)
│   ├── framework_setup_agent.py # Greenfield/brownfield scaffolding
│   └── orchestrator.py           # Master coordinator
├── api/
│   ├── main.py                   # FastAPI application
│   ├── routers/
│   │   ├── requirements.py       # + /upload multipart file ingestion
│   │   ├── tests.py              # + /{id}/data, /{id}/versions diff endpoints
│   │   ├── execution.py          # + suite_type, build_version in RunRequest
│   │   ├── defects.py
│   │   ├── gates.py
│   │   ├── assistant.py          # Streaming AI chat + slash commands
│   │   ├── projects.py           # + /integrations/test endpoint
│   │   ├── healing.py            # Approval queue: list/approve/reject/edit + stats
│   │   ├── test_blocks.py       # Reusable test blocks CRUD
│   │   ├── reports.py           # PDF/XLSX export for runs, coverage, compliance
│   │   ├── dashboard.py         # Live trend data endpoint
│   │   └── exploratory.py       # Charter-based exploratory testing sessions
│   └── models/                   # Pydantic schemas
├── prompts/
│   ├── tea_risk_scoring.txt
│   ├── gherkin_generation.txt
│   ├── root_cause_analysis.txt
│   ├── test_automation_gen.txt
│   └── quality_gate_decision.txt
├── automation/
│   ├── playwright/               # Generated Playwright tests
│   ├── newman/                   # Generated API collections
│   ├── k6/                       # Generated performance scripts
│   └── zap/                      # Generated security configs
├── frontend/
│   ├── src/app/
│   │   ├── dashboard.tsx         # Main dashboard component
│   │   ├── run-history/          # Run history + metrics view
│   │   ├── dashboard/
│   │   ├── architecture/
│   │   ├── explorer/
│   │   ├── assistant/
│   │   ├── defects/
│   │   └── risk/
│   └── components/
│       ├── TraceabilityGraph.tsx  # D3/Force Graph
│       ├── RiskHeatmap.tsx
│       ├── ExecutionFeed.tsx
│       └── AIAssistant.tsx
├── graph/
│   ├── schema.cypher              # Neo4j graph schema
│   └── queries.cypher             # Common traceability queries
├── docker-compose.yml
├── k8s/                           # Kubernetes manifests
└── .arta/                         # Project config
    ├── arta.config.yaml           # Platform + suite + project_type configuration
    ├── quality-gates.yaml         # Gate thresholds
    └── risk-weights.yaml          # Risk scoring weights
```

---

## 19. 4-Outcome Quality Gate Decisions

The Quality Gate Agent produces one of four outcomes for every release assessment:

```
┌────────────┬─────────────────────────────────────────────────────────┐
│ Outcome    │ Condition                                               │
├────────────┼─────────────────────────────────────────────────────────┤
│ PASS       │ All checks clear — safe to deploy                      │
│ CONCERNS   │ Risk score 6-8 with assigned mitigation owners         │
│ FAIL       │ Risk score = 9 OR uncovered P0/P1 acceptance criteria  │
│ WAIVED     │ Authorized exception: rationale + expiry date required │
└────────────┴─────────────────────────────────────────────────────────┘
```

### Gate Waivers

Waivers require admin-only approval and carry:
- **Rationale**: why the risk is acceptable
- **Expiry date**: waiver auto-expires, re-evaluation required
- **Approver**: audit trail of who authorized the waiver
- Stored in `gate_waivers` table with FK to the gate assessment

---

## 20. NFR 4-Category Rubrics

Non-functional requirements are validated against four rubric categories:

| Category | Key Checks | Thresholds |
|----------|-----------|------------|
| **Security** | Auth bypass detection, OWASP top-10 scan, token lifetime | Token expiry ≤ 15 min |
| **Performance** | Response latency, throughput, error rate | p95 < 500 ms, p99 < 1 s, error rate < 1% |
| **Reliability** | Health check endpoints, retry logic, circuit breakers | All services expose `/health`; retries with exponential backoff |
| **Maintainability** | Code coverage, duplication, vulnerability scan | Coverage ≥ 80%, duplication < 5%, zero critical vulns |

Each category produces a rubric score that feeds into the Quality Gate decision.

---

## 21. Test Quality Definition of Done

The TestReviewAgent scores every test suite against 8 mandatory criteria:

| ID | Criterion | Rule |
|----|-----------|------|
| TQ-001 | No hard waits | No `sleep()`, `waitForTimeout()`, or fixed delays |
| TQ-002 | No conditionals in test flow | Tests must not branch with `if`/`else` |
| TQ-003 | Concise | < 300 lines per test file |
| TQ-004 | Fast | < 90 seconds wall-clock execution |
| TQ-005 | Self-cleaning | `afterEach` / teardown resets state |
| TQ-006 | Visible assertions | Every scenario has explicit `expect()` / `assert` |
| TQ-007 | Unique data | Uses faker / uuid — no shared static data |
| TQ-008 | Parallel-safe | No shared mutable state between tests |

**API Endpoint:** `POST /api/tests/review` — submits a test suite for scoring.

---

## 22. Traceability Coverage States

Each acceptance criterion is classified into one of three coverage states:

| State | Definition | Gate Impact |
|-------|-----------|-------------|
| **FULL** | All scenarios covered and passing | No gate impact |
| **PARTIAL** | Some tests exist but coverage is incomplete | Warning for P0/P1 |
| **NONE** | No tests exist for this AC | Triggers gate **FAIL** for P0/P1 requirements |

Coverage state is computed by the Traceability Agent and stored on the `AcceptanceCriteria` node in Neo4j.

---

## 23. Test Review Workflow

```
Test suite submitted
        │
        ▼
  TestReviewAgent scores against 8 TQ criteria
        │
        ▼
  Score returned: pass/fail per criterion + overall score
        │
        ├── All pass → suite approved
        └── Failures → feedback with specific violations + fix suggestions
```

**Endpoint:** `POST /api/tests/review`
**Input:** test suite ID or inline test code
**Output:** per-criterion pass/fail, overall quality score, actionable feedback

---

## 24. Framework Scaffolding

The FrameworkSetupAgent bootstraps test automation for new or existing projects.

### Supported Configurations

| Dimension | Options |
|-----------|---------|
| **Project State** | Greenfield (new), Brownfield (existing) |
| **Stack** | Frontend, Backend, Fullstack |
| **CI Templates** | GitHub Actions, GitLab CI, Jenkins, Azure DevOps, CircleCI |
| **Engagement Model** | TEA Solo (AI-only), TEA Lite (AI + spot review), Integrated (AI + embedded QA) |

The agent generates:
- Directory structure with page objects / fixtures / config
- CI pipeline YAML for the selected provider
- `.arta/arta.config.yaml` pre-filled for the chosen stack
- Sample test files matching the project type

---

## 25. Reusable Test Blocks

Composable test step blocks that can be shared across test scenarios.

### Example: Login Block
```typescript
// Block: login-standard
// Reusable across all test suites requiring authenticated sessions
async function loginBlock(page, { username, password }) {
  await page.goto('/login');
  await page.fill('[data-testid="email"]', username);
  await page.fill('[data-testid="password"]', password);
  await page.click('[data-testid="submit"]');
  await page.waitForURL('/dashboard');
}
```

### CRUD API

```
GET    /api/test-blocks              → list all blocks
POST   /api/test-blocks              → create a new block
GET    /api/test-blocks/{id}         → get block by ID
PUT    /api/test-blocks/{id}         → update block
DELETE /api/test-blocks/{id}         → delete block
```

Blocks can be referenced by ID and inserted into generated test scenarios.

---

## 26. Report Export

PDF and XLSX export endpoints for runs, coverage, and compliance data.

| Endpoint | Formats | Content |
|----------|---------|---------|
| `GET /api/reports/runs/{id}/export?format=pdf\|xlsx` | PDF, XLSX | Run summary, per-test results, evidence links |
| `GET /api/reports/coverage/export?format=pdf\|xlsx` | PDF, XLSX | Requirement-to-test coverage matrix, gap analysis |
| `GET /api/reports/compliance/export?format=pdf\|xlsx` | PDF, XLSX | Gate decisions, NFR rubric scores, waiver log |

---

## 27. Self-Healing Dashboard

Real-time statistics and trends for self-healing activity.

**Endpoint:** `GET /api/healing/stats`

**Returned Metrics:**

| Metric | Description |
|--------|-------------|
| `total_healed` | Total number of tests healed |
| `approval_rate` | Percentage of proposals approved |
| `avg_confidence` | Average AI confidence score |
| `hours_saved` | Estimated manual hours saved |
| `maintenance_reduction_pct` | Reduction in test maintenance effort |
| `weekly_trends` | Healed count per week (last 12 weeks) |
| `strategy_breakdown` | Count by strategy (selector_update, threshold_adjustment, wait_selector) |

---

## 28. Inline AI Suggestions

Context-sensitive AI suggestions surfaced directly in the UI:

| View | Suggestion Type | Trigger |
|------|----------------|---------|
| **Test Explorer** | Edge case suggestions | Viewing a test scenario |
| **Architecture View** | Risk warnings | Any requirement with risk score ≥ 6 |
| **Defect Detail** | Auto root cause analysis | Opening a defect — LLM analyzes failure artifacts |

Suggestions are non-blocking and displayed as inline cards that can be accepted or dismissed.

---

## 29. Multi-Browser Execution

Test runs track browser information per execution result:

- `browser` column added to `execution_results` table (values: `chromium`, `firefox`, `webkit`)
- Run detail view shows per-browser pass/fail breakdown
- Playwright matrix execution across all three browsers in CI

---

## 30. Dashboard Real Data

The main dashboard fetches live trend data instead of relying solely on mock data.

**Endpoint:** `GET /api/dashboard/trends`

Returns:
- Pass/fail trends (last 30 days)
- Coverage trend
- Risk distribution
- Recent gate decisions
- Active defect counts

Charts use live data when available, with graceful fallback to mock data if the API is unreachable.

---

## 31. Exploratory Testing

Charter-based exploratory testing sessions managed through the platform.

### Workflow

```
Create session with charter + scope + time-box
        │
        ▼
  Tester explores and logs findings in real-time
        │
        ▼
  Each finding can be:
    ├── Converted to a defect (linked to requirement)
    └── Converted to a new test case (automated)
        │
        ▼
  Complete session with summary + findings count
```

### API Endpoints

```
POST   /api/exploratory/sessions              → create session
GET    /api/exploratory/sessions/{id}          → get session details
POST   /api/exploratory/sessions/{id}/findings → log a finding
POST   /api/exploratory/findings/{id}/convert  → convert to defect or test case
POST   /api/exploratory/sessions/{id}/complete → complete session with summary
```

---

## 32. Human-in-Loop Self-Heal Approvals

When `require_human_approval: true` in `.arta/arta.config.yaml`, the Self-Healing Agent
queues proposed fixes for human review instead of raising GitHub PRs automatically.

### HealingProposal Data Model

```python
class HealingProposal(BaseModel):
    id: str                          # "heal-001"
    test_id: str                     # "TC-124"
    defect_id: str | None            # Optional linked defect
    strategy: str                    # "selector_update" | "threshold_adjustment" | "wait_selector"
    confidence: float                # 0.0 – 1.0
    ai_reasoning: str                # LLM explanation of the proposed change
    current_selector: str            # Broken selector / current value
    proposed_selector: str           # AI-proposed replacement
    diff_preview: str                # Unified diff preview
    status: Literal["pending", "approved", "rejected"]
    created_at: str                  # ISO 8601
```

### API Endpoints

```
GET  /api/healing/queue                → list proposals (filter by status)
POST /api/healing/{proposal_id}/approve → approve → raises GitHub PR
POST /api/healing/{proposal_id}/reject  → discard, flag as known-broken
POST /api/healing/{proposal_id}/edit    → update proposed_selector then approve
```

### Configuration

```yaml
self_healing:
  require_human_approval: true   # false = auto-PR (original behaviour)
  approval_timeout_hours: 48     # auto-reject if no action taken
  min_confidence_for_queue: 0.65 # below this threshold → not queued
```

### UI — Heal Approval Modal

The Defect Intel view shows a **Heal Queue** badge (count of pending proposals).
Each proposal opens a full-screen modal with:
- Side-by-side diff: red (current broken) vs. green (AI-proposed)
- Confidence bar (colour-coded: green ≥ 0.85, amber 0.65–0.85)
- AI reasoning text
- Actions: **✓ Approve & Raise PR** | **✗ Reject** | **✎ Edit & Approve**
- Edit mode: inline textarea to override the proposed selector before approval

---

## 33. Test Data Generation

ARTA auto-generates JSON fixture tables alongside every Gherkin `Scenario Outline`,
making test data a first-class versioned artifact.

### generate_test_data_fixtures()

```python
async def generate_test_data_fixtures(
    self, gherkin_scenarios: list[str], test_ids: list[str]
) -> list[dict]:
    """
    Parses all Scenario Outline + Examples: blocks in the feature files.
    For each outline, produces a companion JSON fixture:
      {
        "fixture_id": "TD-001",
        "test_id": "TC-124",
        "columns": ["card_type", "amount", "expected_status"],
        "rows": [
          ["VISA",       "99.99",  "confirmed"],
          ["MASTERCARD", "0.01",   "confirmed"],
          ["EXPIRED",    "50.00",  "payment_failed"]
        ],
        "seeded_at": null
      }
    """
```

### API Endpoints

```
GET  /api/tests/{test_id}/data         → returns fixture rows for a test case
POST /api/tests/{test_id}/data         → upsert fixture rows
```

### Database Table

```sql
CREATE TABLE test_data_fixtures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id     TEXT NOT NULL REFERENCES test_cases(test_id),
    fixture_id  TEXT NOT NULL,
    description TEXT,
    columns     JSONB NOT NULL DEFAULT '[]',
    rows        JSONB NOT NULL DEFAULT '[]',
    examples    TEXT,     -- raw Gherkin Examples: block
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

### UI — Test Explorer Tabbed Panel

Test Explorer shows three tabs for each selected test case:

```
[ Code ]  [ Gherkin ]  [ Test Data ]
```

The **Test Data** tab shows:
- Column header row + data rows in a sortable table
- Raw fixture JSON (expandable)
- **Export as CSV** / **Export as JSON** buttons

---

## 34. Test Case Versioning

Every edit to a test case or script auto-snapshots the prior version, enabling
full diff history and one-click restore.

### Database Table

```sql
CREATE TABLE test_case_versions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id          TEXT NOT NULL REFERENCES test_cases(test_id),
    version          INTEGER NOT NULL,
    change_reason    TEXT,       -- "Regenerated after REQ-017 updated"
    gherkin_snapshot TEXT,
    script_snapshot  TEXT,
    changed_by       TEXT DEFAULT 'arta-agent',
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(test_id, version)
);
```

### API Endpoints

```
GET /api/tests/{test_id}/versions              → list version history
GET /api/tests/{test_id}/versions/{v1}/diff/{v2} → unified diff of Gherkin + script
PUT /api/tests/{test_id}                       → auto-snapshots current version before update
```

### Diff Format

```
--- TC-124 v2 (2026-03-09)
+++ TC-124 v3 (2026-03-11)
@@ -5,7 +5,7 @@
   Given the checkout page is loaded
-  When I click "[data-testid='order-confirm']"
+  When I click "[data-testid='confirmation-banner']"
   Then the order confirmation screen is shown
```

### UI — Version History Drawer

The **⎇ History** button in Test Explorer opens a slide-in drawer showing:
- Version list: `v3 (current)`, `v2 (Mar 9)`, `v1 (Mar 5)` with change reason
- Click any version → line-level diff view (red/green)
- **Restore this version** button

---

## 35. Suite Selection and Build Targeting

### Suite Definitions

```yaml
suites:
  smoke:
    priorities: [P0]
    max_duration_s: 300
  regression:
    priorities: [P0, P1]
    max_duration_s: 900
  full:
    priorities: [P0, P1, P2, P3]
```

### RunRequest Extension

```python
class RunRequest(BaseModel):
    suite_type: str = "full"          # smoke | regression | full | custom
    build_version: str | None = None  # run ID to baseline against
    environment: str = "staging"
    tools: list[str] = ["playwright", "newman", "k6"]
```

### UI — Test Explorer Filter Bar

Two-row filter bar replaces the previous single tab row:

```
Row 1 (Suite):  [ Smoke (P0) ]  [ Regression (P0+P1) ]  [ Full ]  [ Custom ]
Row 2 (Status + Actions):  [ All ]  [ Pass ]  [ Fail ]  [ Skip ]  [ ▷ Run Suite ]
```

**Run Suite** opens a **Run Config Modal** with:
- Build version dropdown (run-487, run-486, …)
- Environment (staging / production / dev)
- Tool checkboxes (Playwright / Newman / k6 / ZAP)
- Confirm → streams execution feed

---

## 36. Multi-Format Requirement Ingestion

ARTA accepts requirements in 8 formats beyond plain text, making it compatible with
any existing documentation workflow.

### File Upload Endpoint

```
POST /api/requirements/upload
Content-Type: multipart/form-data
Fields:
  file        — UploadFile (.docx, .xlsx, .pdf, .md, .txt, .json, .yaml)
  source_type — "auto" (default, detect from extension) | explicit format
  project_id  — optional project association

Returns:
  { "requirement_ids": [...], "parsed_count": 3, "warnings": [] }
```

### parse_file() Format Routing

```python
async def parse_file(self, content: bytes, filename: str) -> list[StructuredRequirement]:
    ext = Path(filename).suffix.lower()
    raw_text = {
        ".docx": self._parse_docx,
        ".xlsx": self._parse_xlsx,
        ".pdf":  self._parse_pdf,
        ".md":   self._parse_text,
        ".txt":  self._parse_text,
        ".json": self._parse_json_schema,
        ".yaml": self._parse_yaml_schema,
    }.get(ext, self._parse_text)(content)
    return await self._extract_requirements_from_text(raw_text)
```

### Library Mapping

| Format | Library | Extraction Strategy |
|--------|---------|-------------------|
| `.docx` | `python-docx` | Paragraphs → sections → AC bullets |
| `.xlsx` | `openpyxl` | Rows as requirements, columns as fields |
| `.pdf` | `pdfplumber` | Text extraction → LLM parsing |
| `.md` / `.txt` | built-in | Direct NL parser |
| `.json` / `.yaml` | built-in | Schema detection → structured parse |
| Confluence URL | `atlassian-python-api` | Page HTML → text → NL parser |
| Wiki URL | `httpx` + `beautifulsoup4` | Scrape → text → NL parser |

### UI — AI Assistant Import Panel

The AI Assistant view has a drag-drop **Import Requirements** card:
- Drop zone: "Drop requirements here — .docx · .xlsx · .pdf · .md · Confluence URL"
- Or paste a URL with a **Parse** button
- Progress indicator: "Parsing 3 requirements…" → "✓ REQ-020, REQ-021, REQ-022 created"

---

## 37. Project Types

ARTA supports 5 project types that control default tool selection, scenario generation
strategy, and quality gate thresholds.

### Type Definitions

| Type | Icon | Default Tools | Key Scenarios |
|------|------|--------------|--------------|
| `web_app` | 🌐 | Playwright, Newman, k6, ZAP | E2E, boundary, XSS/SQLi, p95 perf |
| `mobile_app` | 📱 | Appium, Newman, k6 | Accessibility, deep-link, offline, push notifications |
| `api_microservice` | ⬡ | Newman, k6, ZAP | Contract, schema validation, idempotency, rate-limit, auth |
| `analytics` | 📊 | Playwright, Newman | Frozen fixtures, LLM-as-judge, provenance, adversarial, 3-tier |
| `enterprise_app` | 🏢 | All tools + axe-core | Full NFR, accessibility, compliance, penetration testing |

### Analytics Type — TEA-Extended 8 Principles

Analytics projects apply the full TEA-extended methodology:

| Principle | Implementation |
|-----------|---------------|
| **Deterministic** | Frozen dataset snapshots as test fixtures; `temperature=0` for query generation |
| **Isolated** | Separate test files per pipeline layer: NL→Query, Query→Result, Result→Insight, Insight→Narrative |
| **Explicit** | Three assertion categories: numerical tolerance, LLM-as-judge semantic, provenance |
| **Focused** | One test per layer per responsibility; no cross-layer assertions |
| **Fast** | Three-tier execution: @tier1 (≤30s, no LLM), @tier2 (≤5min, SQLite), @tier3 (nightly, full) |
| **Traceable** | Every insight carries trace_id linking model version, prompt version, data snapshot, query |
| **Grounded** | Every claim verifiable from source data: `verify_from_source(insight)` assertion |
| **Adversarial Tested** | Auto-generated adversarial suite: ambiguous metric, conflicting time filter, missing segment, leading question |

### Analytics Gherkin Example

```gherkin
# @tier2 @analytics @grounding
# Provenance: { "data_snapshot": "vendor_spend_Q3_2024.parquet",
#               "model_version": "qwen2.5:32b", "prompt_version": "v3.1" }
Scenario: Top vendor insight is grounded in frozen dataset
  Given frozen dataset "fixtures/vendor_spend_Q3_2024.parquet" is loaded
  When I ask "Who are the top vendors by spend this quarter?"
  Then the top vendor should be "Vendor A"
  And the spend value should be within 1.0% of 4250000
  And the insight should be fully grounded in the source data
  And no hallucinated vendors should appear in the response
```

### Configuration

```yaml
project_types:
  analytics:
    assertion_mode:      "tolerance"
    tolerance_pct:       1.0
    frozen_fixtures:     true
    llm_judge_enabled:   true
    provenance_required: true
    adversarial_suite:   true
    execution_tiers:     [tier1, tier2, tier3]
```

### UI — Project Type Selector

Project Settings has a **5-card radio selector** (icon, name, subtext).
Selecting **Analytics App** reveals an extra options panel:
- Data assertion mode: Tolerance-based / Exact match / LLM-as-judge
- Frozen fixture path input
- Tier checkboxes (Tier 1 / Tier 2 / Tier 3)
- "Generate adversarial edge-case scenarios" toggle

---

## 38. Enhanced Integrations

### Integration Blocks

**GitHub:**
- `repo` — owner/repo (e.g., `myorg/myrepo`)
- `token` — Personal Access Token (masked in API responses)
- `branch` — default branch (default: `main`)
- **⚡ Test Connection** → `POST /api/projects/{id}/integrations/test`

**Jira:**
- `url` — Base URL (e.g., `https://myorg.atlassian.net`)
- `key` — Project key (e.g., `SHOP`)
- `email` — Atlassian account email
- `token` — Jira API Token (masked)
- **⚡ Test Connection**

**Slack:**
- `webhook` — Incoming Webhook URL
- `channel` — Default channel (e.g., `#qa-alerts`)
- **⚡ Send Test Message**

**Microsoft Teams:**
- `webhook` — Teams Incoming Webhook URL
- **⚡ Send Test Message**

### Integration Test Endpoint

```python
POST /api/projects/{project_id}/integrations/test
Body: { "integration": "github" | "jira" | "slack" | "teams" }
Returns: { "ok": bool, "message": str, "latency_ms": int }
```

### Notification Preferences

A matrix table in Project Settings maps events to notification channels:

| Event | Slack | Teams | GitHub Comment |
|-------|-------|-------|---------------|
| Gate: BLOCK | ✓ | ✓ | ✓ |
| Gate: WARN | ✓ | | |
| Self-Heal PR raised | ✓ | | ✓ |
| P0 test failure | ✓ | ✓ | |
| Run completed | | | ✓ |

### Integration Data Model

```json
{
  "github":  { "repo": "", "token": "***", "branch": "main" },
  "jira":    { "url": "", "key": "", "email": "", "token": "***" },
  "slack":   { "webhook": "***", "channel": "#qa-alerts" },
  "teams":   { "webhook": "***" },
  "notifications": {
    "gate_block": ["slack", "teams"],
    "p0_fail":    ["slack", "teams"]
  }
}
```

---

*ARTA is designed to eliminate the gap between writing requirements and shipping quality software — making every engineer a quality engineer and every release a certified release.*
