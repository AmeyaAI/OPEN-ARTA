"""
ARTA Platform — FrameworkSetupAgent
Generates test-framework scaffolding for greenfield and brownfield projects.

Supports:
  - Stack detection: frontend / backend / fullstack
  - Greenfield: create full directory tree + config files from scratch
  - Brownfield: analyze existing structure, identify gaps, recommend additions
  - CI templates for GitHub Actions, GitLab CI, Jenkins, Azure DevOps, CircleCI
"""
from __future__ import annotations

import textwrap
from typing import Any, Literal

# ── CI Templates ────────────────────────────────────────────────────────────

CI_TEMPLATES: dict[str, str] = {
    # ── GitHub Actions ───────────────────────────────────────────────────────
    "github_actions": textwrap.dedent("""\
        name: ARTA Quality Gate
        on:
          push:
            branches: [main, develop]
          pull_request:
            branches: [main]

        concurrency:
          group: ${{ github.workflow }}-${{ github.ref }}
          cancel-in-progress: true

        env:
          NODE_VERSION: "20"
          PYTHON_VERSION: "3.12"

        jobs:
          lint:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-node@v4
                with:
                  node-version: ${{ env.NODE_VERSION }}
                  cache: npm
              - run: npm ci
              - run: npm run lint

          unit-tests:
            runs-on: ubuntu-latest
            needs: lint
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-node@v4
                with:
                  node-version: ${{ env.NODE_VERSION }}
                  cache: npm
              - run: npm ci
              - run: npm run test:unit -- --coverage
              - uses: actions/upload-artifact@v4
                with:
                  name: coverage-report
                  path: coverage/

          e2e-tests:
            runs-on: ubuntu-latest
            needs: unit-tests
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-node@v4
                with:
                  node-version: ${{ env.NODE_VERSION }}
                  cache: npm
              - run: npm ci
              - run: npx playwright install --with-deps chromium
              - run: npm run test:e2e
              - uses: actions/upload-artifact@v4
                if: failure()
                with:
                  name: playwright-report
                  path: test-results/

          api-tests:
            runs-on: ubuntu-latest
            needs: unit-tests
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-node@v4
                with:
                  node-version: ${{ env.NODE_VERSION }}
                  cache: npm
              - run: npm ci
              - run: npm run test:api

          performance-tests:
            runs-on: ubuntu-latest
            needs: [e2e-tests, api-tests]
            steps:
              - uses: actions/checkout@v4
              - uses: grafana/setup-k6-action@v1
              - run: k6 run tests/performance/load-test.js --out json=k6-results.json
              - uses: actions/upload-artifact@v4
                with:
                  name: k6-results
                  path: k6-results.json

          quality-gate:
            runs-on: ubuntu-latest
            needs: [e2e-tests, api-tests, performance-tests]
            steps:
              - uses: actions/checkout@v4
              - name: Call ARTA quality gate
                env:
                  ARTA_API_KEY: ${{ secrets.ARTA_API_KEY }}
                  ARTA_API_BASE_URL: ${{ secrets.ARTA_API_BASE_URL }}
                run: |
                  set -euo pipefail
                  : "${ARTA_API_BASE_URL:=http://localhost:8000}"
                  # F8-11: jq -n --arg quotes the values safely, so a build_id
                  # containing quotes/backslashes can't break the JSON payload.
                  payload=$(jq -n --arg b "${GITHUB_RUN_ID}" --arg e "ci" \\
                    '{build_id:$b, environment:$e}')
                  curl -fsS -X POST \\
                    -H "X-API-Key: ${ARTA_API_KEY}" \\
                    -H "Content-Type: application/json" \\
                    -d "$payload" \\
                    "${ARTA_API_BASE_URL}/api/gates" | tee gate-decision.json
                  decision=$(jq -r .decision gate-decision.json)
                  echo "Gate decision: $decision"
                  test "$decision" != "FAIL"
    """),

    # ── GitLab CI ────────────────────────────────────────────────────────────
    "gitlab_ci": textwrap.dedent("""\
        stages:
          - lint
          - test
          - e2e
          - performance
          - quality-gate

        variables:
          NODE_VERSION: "20"

        .node-setup: &node-setup
          image: node:${NODE_VERSION}-slim
          cache:
            key: ${CI_COMMIT_REF_SLUG}
            paths:
              - node_modules/
          before_script:
            - npm ci --prefer-offline

        lint:
          <<: *node-setup
          stage: lint
          script:
            - npm run lint

        unit-tests:
          <<: *node-setup
          stage: test
          script:
            - npm run test:unit -- --coverage
          artifacts:
            paths:
              - coverage/
            expire_in: 7 days

        e2e-tests:
          <<: *node-setup
          stage: e2e
          script:
            - npx playwright install --with-deps chromium
            - npm run test:e2e
          artifacts:
            when: on_failure
            paths:
              - test-results/
            expire_in: 3 days

        api-tests:
          <<: *node-setup
          stage: e2e
          script:
            - npm run test:api

        performance-tests:
          stage: performance
          image: grafana/k6:latest
          script:
            - k6 run tests/performance/load-test.js --out json=k6-results.json
          artifacts:
            paths:
              - k6-results.json
            expire_in: 7 days

        quality-gate:
          stage: quality-gate
          image: alpine:3.19
          before_script:
            - apk add --no-cache curl jq
          script:
            - 'ARTA_API_BASE_URL="${ARTA_API_BASE_URL:-http://localhost:8000}"'
            - |
              # F8-11: jq -n --arg quotes safely against CI variables that may contain quotes
              payload=$(jq -n --arg b "${CI_PIPELINE_ID}" --arg e "ci" \\
                '{build_id:$b, environment:$e}')
              curl -fsS -X POST \\
                -H "X-API-Key: ${ARTA_API_KEY}" \\
                -H "Content-Type: application/json" \\
                -d "$payload" \\
                "${ARTA_API_BASE_URL}/api/gates" | tee gate-decision.json
            - 'decision=$(jq -r .decision gate-decision.json) && echo "Gate decision: $decision" && test "$decision" != "FAIL"'
          artifacts:
            when: always
            paths: [gate-decision.json]
            expire_in: 30 days
    """),

    # ── Jenkins ──────────────────────────────────────────────────────────────
    "jenkins": textwrap.dedent("""\
        pipeline {
            agent any

            environment {
                NODE_VERSION = '20'
            }

            tools {
                nodejs "${NODE_VERSION}"
            }

            stages {
                stage('Install') {
                    steps {
                        sh 'npm ci'
                    }
                }

                stage('Lint') {
                    steps {
                        sh 'npm run lint'
                    }
                }

                stage('Unit Tests') {
                    steps {
                        sh 'npm run test:unit -- --coverage'
                    }
                    post {
                        always {
                            junit 'test-results/junit.xml'
                            publishHTML target: [
                                reportDir: 'coverage/lcov-report',
                                reportFiles: 'index.html',
                                reportName: 'Coverage Report'
                            ]
                        }
                    }
                }

                stage('E2E Tests') {
                    steps {
                        sh 'npx playwright install --with-deps chromium'
                        sh 'npm run test:e2e'
                    }
                    post {
                        failure {
                            archiveArtifacts artifacts: 'test-results/**', allowEmptyArchive: true
                        }
                    }
                }

                stage('API Tests') {
                    steps {
                        sh 'npm run test:api'
                    }
                }

                stage('Performance Tests') {
                    steps {
                        sh 'k6 run tests/performance/load-test.js --out json=k6-results.json'
                    }
                    post {
                        always {
                            archiveArtifacts artifacts: 'k6-results.json', allowEmptyArchive: true
                        }
                    }
                }

                stage('Quality Gate') {
                    steps {
                        withCredentials([string(credentialsId: 'arta-api-key', variable: 'ARTA_API_KEY')]) {
                            sh '''
                                set -eu
                                : "${ARTA_API_BASE_URL:=http://localhost:8000}"
                                # F8-11: jq -n --arg quotes safely against shell variables
                                payload=$(jq -n --arg b "${BUILD_NUMBER}" --arg e "ci" \\
                                  '{build_id:$b, environment:$e}')
                                curl -fsS -X POST \\
                                  -H "X-API-Key: ${ARTA_API_KEY}" \\
                                  -H "Content-Type: application/json" \\
                                  -d "$payload" \\
                                  "${ARTA_API_BASE_URL}/api/gates" | tee gate-decision.json
                                decision=$(jq -r .decision gate-decision.json)
                                echo "Gate decision: $decision"
                                test "$decision" != "FAIL"
                            '''
                            archiveArtifacts artifacts: 'gate-decision.json', allowEmptyArchive: true
                        }
                    }
                }
            }

            post {
                always {
                    cleanWs()
                }
            }
        }
    """),

    # ── Azure DevOps ─────────────────────────────────────────────────────────
    "azure_devops": textwrap.dedent("""\
        trigger:
          branches:
            include:
              - main
              - develop

        pr:
          branches:
            include:
              - main

        pool:
          vmImage: 'ubuntu-latest'

        variables:
          NODE_VERSION: '20'

        stages:
          - stage: Build
            displayName: 'Build & Lint'
            jobs:
              - job: LintAndBuild
                steps:
                  - task: NodeTool@0
                    inputs:
                      versionSpec: $(NODE_VERSION)
                  - script: npm ci
                    displayName: 'Install dependencies'
                  - script: npm run lint
                    displayName: 'Run linter'

          - stage: Test
            displayName: 'Test Suite'
            dependsOn: Build
            jobs:
              - job: UnitTests
                displayName: 'Unit Tests'
                steps:
                  - task: NodeTool@0
                    inputs:
                      versionSpec: $(NODE_VERSION)
                  - script: npm ci
                  - script: npm run test:unit -- --coverage
                    displayName: 'Run unit tests'
                  - task: PublishTestResults@2
                    inputs:
                      testResultsFormat: 'JUnit'
                      testResultsFiles: 'test-results/junit.xml'
                  - task: PublishCodeCoverageResults@2
                    inputs:
                      codeCoverageTool: 'Cobertura'
                      summaryFileLocation: 'coverage/cobertura-coverage.xml'

              - job: E2ETests
                displayName: 'E2E Tests'
                dependsOn: UnitTests
                steps:
                  - task: NodeTool@0
                    inputs:
                      versionSpec: $(NODE_VERSION)
                  - script: npm ci
                  - script: npx playwright install --with-deps chromium
                    displayName: 'Install Playwright browsers'
                  - script: npm run test:e2e
                    displayName: 'Run E2E tests'
                  - publish: test-results/
                    artifact: playwright-report
                    condition: failed()

              - job: APITests
                displayName: 'API Tests'
                dependsOn: UnitTests
                steps:
                  - task: NodeTool@0
                    inputs:
                      versionSpec: $(NODE_VERSION)
                  - script: npm ci
                  - script: npm run test:api
                    displayName: 'Run API tests'

          - stage: Performance
            displayName: 'Performance Tests'
            dependsOn: Test
            jobs:
              - job: K6Load
                displayName: 'k6 Load Test'
                steps:
                  - script: |
                      curl -sSL https://github.com/grafana/k6/releases/download/v0.50.0/k6-v0.50.0-linux-amd64.tar.gz | tar xz
                      ./k6-v0.50.0-linux-amd64/k6 run tests/performance/load-test.js --out json=k6-results.json
                    displayName: 'Run k6'
                  - publish: k6-results.json
                    artifact: k6-results

          - stage: QualityGate
            displayName: 'Quality Gate'
            dependsOn: Performance
            jobs:
              - job: Gate
                steps:
                  - script: |
                      set -eu
                      : "${ARTA_API_BASE_URL:=http://localhost:8000}"
                      # F8-11: jq -n --arg quotes safely against Azure pipeline variable expansion
                      payload=$(jq -n --arg b "$(Build.BuildId)" --arg e "ci" \\
                        '{build_id:$b, environment:$e}')
                      curl -fsS -X POST \\
                        -H "X-API-Key: $(ARTA_API_KEY)" \\
                        -H "Content-Type: application/json" \\
                        -d "$payload" \\
                        "${ARTA_API_BASE_URL}/api/gates" | tee gate-decision.json
                      decision=$(jq -r .decision gate-decision.json)
                      echo "##vso[task.setvariable variable=GateDecision]$decision"
                      test "$decision" != "FAIL"
                    displayName: 'Assess quality gate'
                    env:
                      ARTA_API_KEY: $(ARTA_API_KEY)
                      ARTA_API_BASE_URL: $(ARTA_API_BASE_URL)
                  - publish: gate-decision.json
                    artifact: gate-decision
                    condition: always()
    """),

    # ── CircleCI ─────────────────────────────────────────────────────────────
    "circleci": textwrap.dedent("""\
        version: 2.1

        orbs:
          node: circleci/node@5.2
          browser-tools: circleci/browser-tools@1.4

        executors:
          node-executor:
            docker:
              - image: cimg/node:20.11
            working_directory: ~/project

        jobs:
          lint:
            executor: node-executor
            steps:
              - checkout
              - node/install-packages
              - run:
                  name: Lint
                  command: npm run lint

          unit-tests:
            executor: node-executor
            steps:
              - checkout
              - node/install-packages
              - run:
                  name: Unit tests
                  command: npm run test:unit -- --coverage
              - store_artifacts:
                  path: coverage
                  destination: coverage-report

          e2e-tests:
            executor: node-executor
            steps:
              - checkout
              - node/install-packages
              - run:
                  name: Install Playwright
                  command: npx playwright install --with-deps chromium
              - run:
                  name: E2E tests
                  command: npm run test:e2e
              - store_artifacts:
                  path: test-results
                  destination: playwright-report
                  when: on_fail

          api-tests:
            executor: node-executor
            steps:
              - checkout
              - node/install-packages
              - run:
                  name: API tests
                  command: npm run test:api

          performance-tests:
            docker:
              - image: grafana/k6:latest
            steps:
              - checkout
              - run:
                  name: k6 load test
                  command: k6 run tests/performance/load-test.js --out json=k6-results.json
              - store_artifacts:
                  path: k6-results.json
                  destination: k6-results

          quality-gate:
            executor: node-executor
            steps:
              - checkout
              - run:
                  name: ARTA Quality Gate
                  command: |
                    set -eu
                    : "${ARTA_API_BASE_URL:=http://localhost:8000}"
                    sudo apt-get update -qq && sudo apt-get install -y -qq jq curl
                    # F8-11: jq -n --arg quotes safely against CircleCI variables
                    payload=$(jq -n --arg b "${CIRCLE_WORKFLOW_ID}" --arg e "ci" \\
                      '{build_id:$b, environment:$e}')
                    curl -fsS -X POST \\
                      -H "X-API-Key: ${ARTA_API_KEY}" \\
                      -H "Content-Type: application/json" \\
                      -d "$payload" \\
                      "${ARTA_API_BASE_URL}/api/gates" | tee gate-decision.json
                    decision=$(jq -r .decision gate-decision.json)
                    echo "Gate decision: $decision"
                    test "$decision" != "FAIL"
              - store_artifacts:
                  path: gate-decision.json
                  destination: gate-decision.json

        workflows:
          arta-pipeline:
            jobs:
              - lint
              - unit-tests:
                  requires: [lint]
              - e2e-tests:
                  requires: [unit-tests]
              - api-tests:
                  requires: [unit-tests]
              - performance-tests:
                  requires: [e2e-tests, api-tests]
              - quality-gate:
                  requires: [performance-tests]
    """),
}

# ── Config File Templates ────────────────────────────────────────────────────

PLAYWRIGHT_CONFIG = textwrap.dedent("""\
    import { defineConfig, devices } from '@playwright/test';

    export default defineConfig({
      testDir: './tests/e2e',
      fullyParallel: true,
      forbidOnly: !!process.env.CI,
      retries: process.env.CI ? 2 : 0,
      workers: process.env.CI ? 1 : undefined,
      reporter: [
        ['html', { open: 'never' }],
        ['json', { outputFile: 'test-results/results.json' }],
        ['junit', { outputFile: 'test-results/junit.xml' }],
      ],
      use: {
        baseURL: process.env.BASE_URL || 'http://localhost:3000',
        trace: 'on-first-retry',
        screenshot: 'only-on-failure',
        video: 'retain-on-failure',
      },
      projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
        { name: 'firefox',  use: { ...devices['Desktop Firefox'] } },
        { name: 'webkit',   use: { ...devices['Desktop Safari'] } },
        { name: 'mobile-chrome', use: { ...devices['Pixel 5'] } },
      ],
    });
""")

K6_CONFIG = textwrap.dedent("""\
    import http from 'k6/http';
    import { check, sleep } from 'k6';
    import { Rate, Trend } from 'k6/metrics';

    const errorRate = new Rate('errors');
    const latency = new Trend('request_latency');

    export const options = {
      stages: [
        { duration: '30s', target: 10 },   // ramp up
        { duration: '1m',  target: 50 },   // sustained load
        { duration: '30s', target: 100 },  // peak
        { duration: '30s', target: 0 },    // ramp down
      ],
      thresholds: {
        http_req_duration: ['p(95)<3000'],  // 95th percentile < 3s
        errors: ['rate<0.01'],              // <1% error rate
      },
    };

    const BASE_URL = __ENV.BASE_URL || 'http://localhost:3000';

    export default function () {
      const res = http.get(`${BASE_URL}/api/health`);
      check(res, {
        'status 200': (r) => r.status === 200,
        'latency < 3s': (r) => r.timings.duration < 3000,
      });
      errorRate.add(res.status !== 200);
      latency.add(res.timings.duration);
      sleep(1);
    }
""")

NEWMAN_COLLECTION_STUB = textwrap.dedent("""\
    {
      "info": {
        "name": "ARTA API Tests",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
      },
      "item": [
        {
          "name": "Health Check",
          "request": {
            "method": "GET",
            "url": "{{baseUrl}}/api/health"
          },
          "event": [
            {
              "listen": "test",
              "script": {
                "exec": [
                  "pm.test('Status 200', () => pm.response.to.have.status(200));"
                ]
              }
            }
          ]
        }
      ],
      "variable": [
        { "key": "baseUrl", "value": "http://localhost:3000" }
      ]
    }
""")

PACKAGE_JSON_TEST_SCRIPTS = {
    "test": "npm run test:unit && npm run test:e2e && npm run test:api",
    "test:unit": "vitest run --coverage",
    "test:e2e": "playwright test",
    "test:api": "newman run tests/api/collection.json -e tests/api/env.json --reporters cli,json --reporter-json-export test-results/api-results.json",
    "test:perf": "k6 run tests/performance/load-test.js",
    "test:security": "zap-cli quick-scan --self-contained http://localhost:3000",
    "lint": "eslint . --ext .ts,.tsx",
}


# ── Stack Directories ────────────────────────────────────────────────────────

_FRONTEND_DIRS = [
    "tests/e2e",
    "tests/e2e/pages",
    "tests/e2e/fixtures",
    "tests/unit",
    "tests/unit/__snapshots__",
    "tests/visual",
    "tests/accessibility",
    "test-results",
]

_BACKEND_DIRS = [
    "tests/api",
    "tests/api/environments",
    "tests/unit",
    "tests/integration",
    "tests/performance",
    "tests/security",
    "test-results",
]

_FULLSTACK_DIRS = sorted(set(_FRONTEND_DIRS + _BACKEND_DIRS))


# ── FrameworkSetupAgent ──────────────────────────────────────────────────────


StackType = Literal["frontend", "backend", "fullstack"]
EngagementModel = Literal["tea_solo", "tea_lite", "integrated"]
CIProvider = Literal["github_actions", "gitlab_ci", "jenkins", "azure_devops", "circleci"]


class FrameworkSetupAgent:
    """
    Generates test-framework scaffolding based on project configuration.

    Usage::

        agent = FrameworkSetupAgent()
        result = agent.scaffold({
            "name": "My App",
            "stack_type": "fullstack",
            "is_greenfield": True,
            "engagement_model": "tea_solo",
            "ci_provider": "github_actions",
        })
        # result => {"files": {path: content, ...}, "directories": [...], "recommendations": [...]}
    """

    # ── Public API ───────────────────────────────────────────────────────────

    def scaffold(self, project: dict[str, Any]) -> dict[str, Any]:
        """
        Generate the scaffolding plan for the given project configuration.

        Parameters
        ----------
        project : dict
            Required keys:
              - name: str
              - stack_type: "frontend" | "backend" | "fullstack"
              - is_greenfield: bool  (True = create from scratch, False = analyze & fill gaps)
              - engagement_model: "tea_solo" | "tea_lite" | "integrated"
              - ci_provider: "github_actions" | "gitlab_ci" | "jenkins" | "azure_devops" | "circleci"

        Returns
        -------
        dict with keys:
          - files: dict[str, str]          — file path → file content
          - directories: list[str]          — directories to create
          - recommendations: list[str]      — human-readable guidance
        """
        name: str = project.get("name", "my-project")
        stack_type: StackType = project.get("stack_type", "fullstack")
        is_greenfield: bool = project.get("is_greenfield", True)
        engagement_model: EngagementModel = project.get("engagement_model", "tea_solo")
        ci_provider: CIProvider = project.get("ci_provider", "github_actions")

        files: dict[str, str] = {}
        directories: list[str] = []
        recommendations: list[str] = []

        # ── 1. Directories ───────────────────────────────────────────────────
        directories = self._directories_for_stack(stack_type)

        # ── 2. Config files ──────────────────────────────────────────────────
        if is_greenfield:
            files.update(self._greenfield_configs(name, stack_type, ci_provider))
        else:
            files.update(self._brownfield_configs(name, stack_type, ci_provider))

        # ── 3. CI config ─────────────────────────────────────────────────────
        ci_path = self._ci_file_path(ci_provider)
        files[ci_path] = CI_TEMPLATES[ci_provider]

        # ── 4. Recommendations ───────────────────────────────────────────────
        recommendations = self._build_recommendations(
            name, stack_type, is_greenfield, engagement_model, ci_provider,
        )

        return {
            "files": files,
            "directories": directories,
            "recommendations": recommendations,
        }

    # ── Private helpers ──────────────────────────────────────────────────────

    def render_ci_config(
        self,
        project: dict[str, Any],
        tools: list[str] | None = None,
        ci_provider: CIProvider | None = None,
    ) -> dict[str, str]:
        """BMAD Layer 1+4 deliverable: Tool Chain + per-project CI pipeline.

        Returns {"path": ".github/workflows/...", "content": "<yaml>"}.
        Tool list is rendered as a header comment so engineers reviewing the
        config can see WHICH tools the project owns at a glance. The body
        of the template is unchanged from the canonical CI_TEMPLATES — same
        stages, same security model, just project-name interpolation and
        tool-chain documentation.
        """
        provider: CIProvider = ci_provider or project.get("ci_provider", "github_actions")
        if provider not in CI_TEMPLATES:
            raise ValueError(f"Unknown ci_provider: {provider!r}")
        path = self._ci_file_path(provider)
        body = CI_TEMPLATES[provider]
        tool_list = ", ".join(sorted(tools or [])) or "(none configured)"
        header = (
            f"# ARTA-generated CI config for project '{project.get('name', '?')}'\n"
            f"# Tool chain (BMAD Layer 1): {tool_list}\n"
            f"# Provider: {provider}\n"
        )
        return {"path": path, "content": header + body}

    @staticmethod
    def _directories_for_stack(stack_type: StackType) -> list[str]:
        if stack_type == "frontend":
            return list(_FRONTEND_DIRS)
        elif stack_type == "backend":
            return list(_BACKEND_DIRS)
        return list(_FULLSTACK_DIRS)

    @staticmethod
    def _ci_file_path(ci_provider: CIProvider) -> str:
        mapping = {
            "github_actions": ".github/workflows/arta-quality.yml",
            "gitlab_ci": ".gitlab-ci.yml",
            "jenkins": "Jenkinsfile",
            "azure_devops": "azure-pipelines.yml",
            "circleci": ".circleci/config.yml",
        }
        return mapping[ci_provider]

    def _greenfield_configs(
        self, name: str, stack_type: StackType, ci_provider: CIProvider,
    ) -> dict[str, str]:
        """Full config file set for a brand-new project."""
        files: dict[str, str] = {}

        # Playwright config — frontend or fullstack
        if stack_type in ("frontend", "fullstack"):
            files["playwright.config.ts"] = PLAYWRIGHT_CONFIG
            files["tests/e2e/example.spec.ts"] = self._example_e2e_spec(name)

        # k6 performance config
        files["tests/performance/load-test.js"] = K6_CONFIG

        # Newman API collection — backend or fullstack
        if stack_type in ("backend", "fullstack"):
            files["tests/api/collection.json"] = NEWMAN_COLLECTION_STUB
            files["tests/api/environments/local.json"] = textwrap.dedent("""\
                {
                  "id": "local",
                  "name": "Local",
                  "values": [
                    { "key": "baseUrl", "value": "http://localhost:3000", "enabled": true }
                  ]
                }
            """)

        # package.json test scripts (as a merge fragment)
        import json
        files["package.json.arta-scripts"] = json.dumps(
            {"scripts": PACKAGE_JSON_TEST_SCRIPTS}, indent=2,
        )

        # ARTA project config
        files[".arta/arta.config.yaml"] = self._arta_config_yaml(name, stack_type, ci_provider)

        # .env.test template
        files[".env.test"] = textwrap.dedent("""\
            # ARTA Test Environment
            BASE_URL=http://localhost:3000
            API_URL=http://localhost:3000/api
            NODE_ENV=test
            CI=true
        """)

        return files

    def _brownfield_configs(
        self, name: str, stack_type: StackType, ci_provider: CIProvider,
    ) -> dict[str, str]:
        """
        For brownfield projects, generate only the files that are typically
        missing. The caller should diff against existing project structure.
        """
        files: dict[str, str] = {}

        # Always provide ARTA config
        files[".arta/arta.config.yaml"] = self._arta_config_yaml(name, stack_type, ci_provider)

        # Playwright config if frontend/fullstack (often missing or outdated)
        if stack_type in ("frontend", "fullstack"):
            files["playwright.config.ts"] = PLAYWRIGHT_CONFIG

        # k6 — commonly absent in brownfield
        files["tests/performance/load-test.js"] = K6_CONFIG

        # .env.test — safe to generate (gitignored)
        files[".env.test"] = textwrap.dedent("""\
            # ARTA Test Environment
            BASE_URL=http://localhost:3000
            API_URL=http://localhost:3000/api
            NODE_ENV=test
            CI=true
        """)

        return files

    @staticmethod
    def _example_e2e_spec(name: str) -> str:
        safe_name = name.replace("'", "\\'")
        return textwrap.dedent(f"""\
            import {{ test, expect }} from '@playwright/test';

            test.describe('{safe_name} — smoke tests', () => {{
              test('homepage loads successfully', async ({{ page }}) => {{
                await page.goto('/');
                await expect(page).toHaveTitle(/.+/);
              }});

              test('health endpoint returns 200', async ({{ request }}) => {{
                const res = await request.get('/api/health');
                expect(res.ok()).toBeTruthy();
              }});
            }});
        """)

    @staticmethod
    def _arta_config_yaml(name: str, stack_type: StackType, ci_provider: CIProvider) -> str:
        return textwrap.dedent(f"""\
            # ARTA Platform Configuration
            # Generated by FrameworkSetupAgent

            project:
              name: "{name}"
              stack_type: "{stack_type}"

            tea:
              engagement_model: tea_solo
              risk_threshold: 8.0        # P0 if risk >= 8.0
              coverage_target: 100        # % for P0 requirements
              auto_heal: true

            automation:
              ui_tool: playwright
              api_tool: newman
              perf_tool: k6
              security_tool: zap

            ci:
              provider: "{ci_provider}"
              gate_on_pr: true
              block_on_p0_fail: true

            quality_gates:
              p0_pass_rate: 100
              p0_coverage: 100
              p1_coverage: 90
              overall_coverage: 80
              performance_p95_ms: 3000
              max_open_p0_defects: 0
              max_critical_security: 0
        """)

    @staticmethod
    def _build_recommendations(
        name: str,
        stack_type: StackType,
        is_greenfield: bool,
        engagement_model: EngagementModel,
        ci_provider: CIProvider,
    ) -> list[str]:
        recs: list[str] = []

        if is_greenfield:
            recs.append(
                "Greenfield setup: all config files and directory structure have been generated. "
                "Merge the scripts from package.json.arta-scripts into your package.json."
            )
        else:
            recs.append(
                "Brownfield setup: only missing/gap files have been generated. "
                "Review each file before committing — some may need manual merging with existing configs."
            )

        if stack_type == "frontend":
            recs.append(
                "Frontend stack detected: Playwright E2E + visual regression tests are configured. "
                "Consider adding Axe-core accessibility checks in tests/accessibility/."
            )
        elif stack_type == "backend":
            recs.append(
                "Backend stack detected: Newman API tests + k6 performance tests are configured. "
                "Add contract tests (Pact) if you consume or expose APIs to other services."
            )
        else:
            recs.append(
                "Fullstack stack detected: both Playwright E2E and Newman API tests are configured. "
                "Ensure BASE_URL and API_URL in .env.test point to your running application."
            )

        engagement_advice = {
            "tea_solo": (
                "TEA Solo engagement: ARTA will autonomously generate tests, run them, "
                "and enforce quality gates. Human review is recommended for P0 requirements."
            ),
            "tea_lite": (
                "TEA Lite engagement: ARTA generates test suggestions but a human approves "
                "before automation. Set up PR-based review workflow."
            ),
            "integrated": (
                "Integrated engagement: ARTA works alongside your existing QA team. "
                "Use /generate-tests for AI suggestions and /check-coverage for gap analysis."
            ),
        }
        recs.append(engagement_advice.get(engagement_model, engagement_advice["tea_solo"]))

        ci_advice = {
            "github_actions": "GitHub Actions workflow created at .github/workflows/arta-quality.yml.",
            "gitlab_ci": "GitLab CI pipeline created at .gitlab-ci.yml.",
            "jenkins": "Jenkinsfile created at project root.",
            "azure_devops": "Azure DevOps pipeline created at azure-pipelines.yml.",
            "circleci": "CircleCI config created at .circleci/config.yml.",
        }
        recs.append(ci_advice[ci_provider])

        recs.append(
            "Next steps: (1) Review generated configs, (2) Set environment variables in .env.test, "
            "(3) Run 'npm run test' to verify the scaffold, (4) Use /run-atdd to start the TEA pipeline."
        )

        return recs
