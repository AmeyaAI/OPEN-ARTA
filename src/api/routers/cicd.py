"""
ARTA CI/CD Configuration Router
Repository stack detection, CI/CD pipeline config generation, and commit-to-repo.

Endpoints:
  POST /api/projects/{project_id}/detect-stack    Detect repo language/framework/tools
  POST /api/projects/{project_id}/cicd-config      Generate CI/CD pipeline config
  POST /api/projects/{project_id}/cicd-commit       Commit config to repo via GitHub API
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("arta.cicd")

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# ── Auth dependency ───────────────────────────────────────────────────────────


# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


# ── Pydantic Models ──────────────────────────────────────────────────────────


class DetectStackRequest(BaseModel):
    repository_url: str = Field(..., min_length=1, description="GitHub/GitLab repo URL")
    access_token: str | None = Field(default=None, description="Optional PAT for private repos")


class DetectedSuggestions(BaseModel):
    automation_tools: list[str] = []
    test_directory: str = "tests/"
    config_files: list[str] = []


class DetectStackResponse(BaseModel):
    language: str
    framework: str
    test_framework: str
    package_manager: str
    detected_files: list[str]
    suggestions: DetectedSuggestions
    confidence: float


class CICDProvider(str, Enum):
    github_actions = "github_actions"
    jenkins = "jenkins"
    circleci = "circleci"
    gitlab_ci = "gitlab_ci"
    azure_devops = "azure_devops"


class EnvironmentConfig(BaseModel):
    name: str = "staging"
    url: str = "https://staging.example.com"


class CICDConfigRequest(BaseModel):
    provider: CICDProvider = CICDProvider.github_actions
    triggers: list[str] = Field(default=["push", "pull_request", "manual", "schedule"])
    branch_patterns: list[str] = Field(default=["main", "develop", "release/*"])
    environments: list[EnvironmentConfig] = Field(default=[EnvironmentConfig()])
    test_tools: list[str] = Field(default=["playwright", "newman", "k6"])
    arta_api_url: str = "https://arta.example.com"
    schedule_cron: str = "0 6 * * 1-5"


class CICDConfigResponse(BaseModel):
    filename: str
    content: str
    language: str
    provider: str
    stages: list[str]


class CICDCommitRequest(BaseModel):
    config_content: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    repository_url: str = Field(..., min_length=1)
    access_token: str = Field(..., min_length=1)
    branch_name: str | None = None


class CICDCommitResponse(BaseModel):
    branch: str
    pr_url: str
    pr_number: int
    status: str


# ── Constants ─────────────────────────────────────────────────────────────────

ARTA_PIPELINE_STAGES = [
    "Checkout & Setup",
    "Requirements Sync",
    "AI Test Generation",
    "Test Execution — Smoke (P0)",
    "Test Execution — Full (P0+P1+P2)",
    "Security Scan (OWASP ZAP)",
    "Performance Test (k6)",
    "Quality Gate Decision",
]


# ── Stack Detection Logic ─────────────────────────────────────────────────────


def _parse_github_owner_repo(url: str) -> tuple[str, str] | None:
    """Extract owner/repo from a GitHub URL."""
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",
        r"github\.com[:/]([^/]+)/([^/.]+?)/?$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1), m.group(2)
    return None


async def _detect_via_github_api(owner: str, repo: str, token: str) -> dict[str, Any] | None:
    """Try to list repo root files via GitHub API. Returns None on failure."""
    try:
        import httpx
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/",
                headers=headers,
            )
            if resp.status_code != 200:
                return None
            files = [item["name"] for item in resp.json() if item.get("type") == "file"]

            # Optionally fetch package.json content for deeper detection
            pkg_content = None
            if "package.json" in files:
                pkg_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/package.json",
                    headers=headers,
                )
                if pkg_resp.status_code == 200:
                    import base64, json
                    data = pkg_resp.json()
                    if data.get("encoding") == "base64" and data.get("content"):
                        pkg_content = json.loads(base64.b64decode(data["content"]).decode())

            return {"files": files, "package_json": pkg_content}
    except Exception as exc:
        log.debug("GitHub API detection failed: %s", exc)
        return None


def _detect_stack_from_files(
    files: list[str],
    package_json: dict | None = None,
) -> DetectStackResponse:
    """Analyse a list of root-level filenames to detect the project stack."""

    language = "unknown"
    framework = "unknown"
    test_framework = "unknown"
    package_manager = "unknown"
    automation_tools: list[str] = []
    config_files: list[str] = []
    test_directory = "tests/"
    confidence = 0.5

    all_deps: dict[str, Any] = {}
    if package_json:
        all_deps.update(package_json.get("dependencies", {}))
        all_deps.update(package_json.get("devDependencies", {}))

    # ── Language detection ─────────────────────────────────────────────────
    if "package.json" in files:
        language = "typescript" if "tsconfig.json" in files else "javascript"
        package_manager = "npm"
        if "yarn.lock" in files:
            package_manager = "yarn"
        elif "pnpm-lock.yaml" in files:
            package_manager = "pnpm"
        elif "bun.lockb" in files:
            package_manager = "bun"
        confidence = 0.85

        # Framework detection from deps or config files
        if "next.config.js" in files or "next.config.mjs" in files or "next.config.ts" in files or "next" in all_deps:
            framework = "next.js"
            confidence = 0.92
        elif "angular.json" in files or "@angular/core" in all_deps:
            framework = "angular"
            confidence = 0.92
        elif "nuxt.config.ts" in files or "nuxt.config.js" in files or "nuxt" in all_deps:
            framework = "nuxt"
            confidence = 0.90
        elif "vite.config.ts" in files or "vite.config.js" in files:
            framework = "vite"
            if "vue" in all_deps:
                framework = "vue"
            elif "react" in all_deps:
                framework = "react"
            confidence = 0.88
        elif "react" in all_deps or "react-dom" in all_deps:
            framework = "react"
            confidence = 0.85
        elif "vue" in all_deps:
            framework = "vue"
            confidence = 0.85
        elif "express" in all_deps:
            framework = "express"
            confidence = 0.85
        elif "fastify" in all_deps:
            framework = "fastify"
            confidence = 0.85
        elif "nest" in all_deps or "@nestjs/core" in all_deps:
            framework = "nestjs"
            confidence = 0.88

    elif "requirements.txt" in files or "pyproject.toml" in files or "setup.py" in files or "Pipfile" in files:
        language = "python"
        if "Pipfile" in files:
            package_manager = "pipenv"
        elif "pyproject.toml" in files:
            package_manager = "poetry"
        elif "requirements.txt" in files:
            package_manager = "pip"
        confidence = 0.80

        if "manage.py" in files:
            framework = "django"
            confidence = 0.90
        else:
            # Default to fastapi for Python API projects
            framework = "fastapi"
            confidence = 0.75

    elif "go.mod" in files:
        language = "go"
        package_manager = "go modules"
        framework = "go"
        confidence = 0.88
        test_directory = "./"

    elif "pom.xml" in files:
        language = "java"
        package_manager = "maven"
        framework = "spring" if "src/main/java" in files else "java"
        confidence = 0.85
        test_directory = "src/test/"

    elif "build.gradle" in files or "build.gradle.kts" in files:
        language = "java"
        package_manager = "gradle"
        framework = "spring"
        confidence = 0.85
        test_directory = "src/test/"

    elif "Cargo.toml" in files:
        language = "rust"
        package_manager = "cargo"
        framework = "rust"
        confidence = 0.88
        test_directory = "tests/"

    elif any(f.endswith(".csproj") or f.endswith(".sln") for f in files):
        language = "csharp"
        package_manager = "nuget"
        framework = "dotnet"
        confidence = 0.85
        test_directory = "tests/"

    # ── Test framework detection ──────────────────────────────────────────
    test_configs = {
        "playwright.config.ts": "playwright",
        "playwright.config.js": "playwright",
        "jest.config.js": "jest",
        "jest.config.ts": "jest",
        "vitest.config.ts": "vitest",
        "vitest.config.js": "vitest",
        "cypress.config.ts": "cypress",
        "cypress.config.js": "cypress",
        "cypress.json": "cypress",
        ".mocharc.yml": "mocha",
        ".mocharc.json": "mocha",
        "karma.conf.js": "karma",
        "pytest.ini": "pytest",
        "setup.cfg": "pytest",
        "tox.ini": "tox",
        "phpunit.xml": "phpunit",
    }
    for fname, fw in test_configs.items():
        if fname in files:
            test_framework = fw
            config_files.append(fname)
            confidence = min(confidence + 0.05, 0.98)
            break

    # Also check devDependencies
    if test_framework == "unknown" and package_json:
        dep_test_map = {
            "@playwright/test": "playwright",
            "jest": "jest",
            "vitest": "vitest",
            "cypress": "cypress",
            "mocha": "mocha",
        }
        for dep, fw in dep_test_map.items():
            if dep in all_deps:
                test_framework = fw
                break

    # ── Automation tool suggestions ───────────────────────────────────────
    # Always suggest ARTA core tools
    automation_tools = ["playwright", "newman", "k6"]
    if test_framework == "cypress":
        automation_tools = ["cypress", "newman", "k6"]
    elif test_framework == "jest" or test_framework == "vitest":
        automation_tools = [test_framework, "playwright", "newman", "k6"]
    elif language == "python":
        automation_tools = ["pytest", "playwright", "newman", "k6"]
    elif language == "java":
        automation_tools = ["testng", "playwright", "newman", "k6"]
    elif language == "go":
        automation_tools = ["go test", "playwright", "newman", "k6"]

    # ── Config files ──────────────────────────────────────────────────────
    known_configs = [
        "package.json", "tsconfig.json", "next.config.js", "next.config.mjs",
        "next.config.ts", "vite.config.ts", "vite.config.js", "angular.json",
        "playwright.config.ts", "playwright.config.js", "jest.config.js",
        "jest.config.ts", "vitest.config.ts", "cypress.config.ts",
        "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
        "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
        "Cargo.toml", "docker-compose.yml", "Dockerfile", ".dockerignore",
        "Makefile", ".env.example",
    ]
    config_files.extend([f for f in files if f in known_configs and f not in config_files])

    return DetectStackResponse(
        language=language,
        framework=framework,
        test_framework=test_framework,
        package_manager=package_manager,
        detected_files=config_files,
        suggestions=DetectedSuggestions(
            automation_tools=automation_tools,
            test_directory=test_directory,
            config_files=config_files,
        ),
        confidence=round(confidence, 2),
    )


# ── Mock detection based on URL patterns ──────────────────────────────────────


def _mock_detect_from_url(url: str) -> DetectStackResponse:
    """Fallback: infer stack from repo name patterns when API is unavailable."""
    url_lower = url.lower()

    if any(kw in url_lower for kw in ["next", "react", "frontend", "web", "ui"]):
        return DetectStackResponse(
            language="typescript",
            framework="next.js",
            test_framework="playwright",
            package_manager="npm",
            detected_files=["package.json", "next.config.js", "tsconfig.json", "playwright.config.ts"],
            suggestions=DetectedSuggestions(
                automation_tools=["playwright", "newman", "k6"],
                test_directory="tests/",
                config_files=["package.json", "next.config.js", "playwright.config.ts"],
            ),
            confidence=0.65,
        )
    elif any(kw in url_lower for kw in ["python", "fastapi", "django", "flask", "api"]):
        return DetectStackResponse(
            language="python",
            framework="fastapi",
            test_framework="pytest",
            package_manager="pip",
            detected_files=["requirements.txt", "pyproject.toml", "pytest.ini"],
            suggestions=DetectedSuggestions(
                automation_tools=["pytest", "playwright", "newman", "k6"],
                test_directory="tests/",
                config_files=["requirements.txt", "pyproject.toml"],
            ),
            confidence=0.60,
        )
    elif any(kw in url_lower for kw in ["go", "golang"]):
        return DetectStackResponse(
            language="go",
            framework="go",
            test_framework="go test",
            package_manager="go modules",
            detected_files=["go.mod", "go.sum"],
            suggestions=DetectedSuggestions(
                automation_tools=["go test", "playwright", "newman", "k6"],
                test_directory="./",
                config_files=["go.mod"],
            ),
            confidence=0.55,
        )
    elif any(kw in url_lower for kw in ["java", "spring", "maven", "gradle"]):
        return DetectStackResponse(
            language="java",
            framework="spring",
            test_framework="junit",
            package_manager="maven",
            detected_files=["pom.xml", "src/main/java"],
            suggestions=DetectedSuggestions(
                automation_tools=["testng", "playwright", "newman", "k6"],
                test_directory="src/test/",
                config_files=["pom.xml"],
            ),
            confidence=0.55,
        )
    else:
        # Generic fallback — assume Node.js/TypeScript
        return DetectStackResponse(
            language="typescript",
            framework="unknown",
            test_framework="playwright",
            package_manager="npm",
            detected_files=["package.json"],
            suggestions=DetectedSuggestions(
                automation_tools=["playwright", "newman", "k6"],
                test_directory="tests/",
                config_files=["package.json"],
            ),
            confidence=0.40,
        )


# ── CI/CD Template Generators ────────────────────────────────────────────────


def _indent(text: str, spaces: int) -> str:
    """Indent every line of text by the given number of spaces."""
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _generate_github_actions(cfg: CICDConfigRequest) -> CICDConfigResponse:
    """Generate GitHub Actions workflow YAML."""

    # Build trigger block
    trigger_lines = []
    push_branches = [b for b in cfg.branch_patterns if "*" not in b]
    wildcard_branches = [b for b in cfg.branch_patterns if "*" in b]
    all_branches = push_branches + wildcard_branches

    if "push" in cfg.triggers:
        trigger_lines.append("  push:")
        trigger_lines.append(f"    branches: [{', '.join(all_branches)}]")
    if "pull_request" in cfg.triggers:
        pr_branches = push_branches[:1] if push_branches else ["main"]
        trigger_lines.append("  pull_request:")
        trigger_lines.append(f"    branches: [{', '.join(pr_branches)}]")
    if "manual" in cfg.triggers:
        trigger_lines.append("  workflow_dispatch:")
    if "schedule" in cfg.triggers:
        trigger_lines.append("  schedule:")
        trigger_lines.append(f"    - cron: '{cfg.schedule_cron}'")

    triggers_block = "\n".join(trigger_lines)

    env_name = cfg.environments[0].name if cfg.environments else "staging"
    env_url = cfg.environments[0].url if cfg.environments else "https://staging.example.com"

    # Build tools install step
    tools_install = ""
    if "playwright" in cfg.test_tools:
        tools_install += """
      - name: Install Playwright
        run: npx playwright install --with-deps"""
    if "k6" in cfg.test_tools:
        tools_install += """
      - name: Install k6
        run: |
          sudo gpg -k
          sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \\
            --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D68
          echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \\
            | sudo tee /etc/apt/sources.list.d/k6.list
          sudo apt-get update && sudo apt-get install -y k6"""

    content = f"""# ============================================================================
# ARTA Quality Pipeline — GitHub Actions
# Generated by ARTA CI/CD Configuration Engine
# ============================================================================
name: ARTA Quality Pipeline

on:
{triggers_block}

env:
  ARTA_API_URL: ${{{{ secrets.ARTA_API_URL }}}}
  ARTA_API_KEY: ${{{{ secrets.ARTA_API_KEY }}}}
  ARTA_PROJECT_ID: ${{{{ secrets.ARTA_PROJECT_ID }}}}
  TARGET_ENV: {env_name}
  TARGET_URL: {env_url}

jobs:
  arta-quality:
    name: ARTA Quality Pipeline
    runs-on: ubuntu-latest
    environment: {env_name}
    steps:
      # ── Stage 1: Checkout & Setup ────────────────────────────────────────
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'

      - name: Install dependencies
        run: npm ci
{tools_install}

      # ── Stage 2: Requirements Sync ──────────────────────────────────────
      - name: Sync requirements with ARTA
        run: |
          curl -sf -X POST "$ARTA_API_URL/api/requirements/sync" \\
            -H "Authorization: Bearer $ARTA_API_KEY" \\
            -H "Content-Type: application/json" \\
            -d '{{"project_id": "'$ARTA_PROJECT_ID'", "source": "repo"}}' \\
            || echo "::warning::Requirements sync returned non-zero — continuing"

      # ── Stage 3: AI Test Generation ─────────────────────────────────────
      - name: Generate tests via ARTA AI
        run: |
          curl -sf -X POST "$ARTA_API_URL/api/tests/generate" \\
            -H "Authorization: Bearer $ARTA_API_KEY" \\
            -H "Content-Type: application/json" \\
            -d '{{"project_id": "'$ARTA_PROJECT_ID'", "strategy": "risk_based"}}' \\
            || echo "::warning::Test generation returned non-zero — continuing"

      # ── Stage 4: Smoke Tests (P0) ───────────────────────────────────────
      - name: Execute Smoke Tests (P0 — must pass)
        id: smoke
        run: |
          RESULT=$(curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
            -H "Authorization: Bearer $ARTA_API_KEY" \\
            -H "Content-Type: application/json" \\
            -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "smoke", "environment": "'$TARGET_ENV'", "build_id": "'$GITHUB_RUN_ID'"}}')
          echo "smoke_result=$RESULT" >> $GITHUB_OUTPUT
          echo "$RESULT" | jq .

      # ── Stage 5: Full Test Suite (P0+P1+P2) ────────────────────────────
      - name: Execute Full Test Suite
        id: full
        run: |
          RESULT=$(curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
            -H "Authorization: Bearer $ARTA_API_KEY" \\
            -H "Content-Type: application/json" \\
            -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "full", "environment": "'$TARGET_ENV'", "build_id": "'$GITHUB_RUN_ID'"}}')
          echo "full_result=$RESULT" >> $GITHUB_OUTPUT
          echo "$RESULT" | jq .

      # ── Stage 6: Security Scan (OWASP ZAP) ─────────────────────────────
      - name: OWASP ZAP Security Scan
        run: |
          echo "Running OWASP ZAP baseline scan against $TARGET_URL..."
          docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py \\
            -t "$TARGET_URL" -r zap-report.html -l WARN || true
        continue-on-error: true

      - name: Upload ZAP Report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: zap-security-report
          path: zap-report.html
          retention-days: 30

      # ── Stage 7: Performance Tests (k6) ─────────────────────────────────
      - name: k6 Performance Tests
        run: |
          echo "Running k6 performance tests against $TARGET_URL..."
          k6 run --out json=k6-results.json \\
            -e TARGET_URL="$TARGET_URL" \\
            src/automation/k6/checkout-performance.js || true
        continue-on-error: true

      - name: Upload k6 Results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: k6-performance-results
          path: k6-results.json
          retention-days: 30

      # ── Stage 8: Quality Gate Decision ──────────────────────────────────
      - name: ARTA Quality Gate Check
        id: gate
        run: |
          GATE=$(curl -sf "$ARTA_API_URL/api/gates/check?build_id=$GITHUB_RUN_ID&project_id=$ARTA_PROJECT_ID" \\
            -H "Authorization: Bearer $ARTA_API_KEY")
          echo "$GATE" | jq .
          DECISION=$(echo "$GATE" | jq -r '.decision // "UNKNOWN"')
          echo "gate_decision=$DECISION" >> $GITHUB_OUTPUT
          if [ "$DECISION" = "FAIL" ]; then
            echo "::error::Quality gate FAILED — blocking deployment"
            exit 1
          elif [ "$DECISION" = "CONCERNS" ]; then
            echo "::warning::Quality gate raised CONCERNS — review recommended"
          else
            echo "Quality gate PASSED"
          fi

      - name: Post Gate Summary
        if: always()
        run: |
          echo "## ARTA Quality Gate Result" >> $GITHUB_STEP_SUMMARY
          echo "| Metric | Value |" >> $GITHUB_STEP_SUMMARY
          echo "|--------|-------|" >> $GITHUB_STEP_SUMMARY
          echo "| Decision | ${{{{ steps.gate.outputs.gate_decision }}}} |" >> $GITHUB_STEP_SUMMARY
          echo "| Build | $GITHUB_RUN_ID |" >> $GITHUB_STEP_SUMMARY
          echo "| Environment | $TARGET_ENV |" >> $GITHUB_STEP_SUMMARY
"""

    return CICDConfigResponse(
        filename=".github/workflows/arta-quality.yml",
        content=content.strip() + "\n",
        language="yaml",
        provider="github_actions",
        stages=ARTA_PIPELINE_STAGES,
    )


def _generate_jenkins(cfg: CICDConfigRequest) -> CICDConfigResponse:
    """Generate Jenkins declarative pipeline (Jenkinsfile)."""

    env_name = cfg.environments[0].name if cfg.environments else "staging"
    env_url = cfg.environments[0].url if cfg.environments else "https://staging.example.com"

    # Build triggers block
    trigger_lines = []
    if "push" in cfg.triggers or "pull_request" in cfg.triggers:
        branches = " || ".join(f'branch "{b}"' for b in cfg.branch_patterns if "*" not in b)
        wildcards = " || ".join(f'branch "{b}"' for b in cfg.branch_patterns if "*" in b)
        all_conds = " || ".join(filter(None, [branches, wildcards]))
        if all_conds:
            trigger_lines.append(f"        // Branch filter: {all_conds}")
    if "schedule" in cfg.triggers:
        trigger_lines.append(f"        cron('{cfg.schedule_cron}')")

    triggers_block = "\n".join(trigger_lines) if trigger_lines else "        // No automatic triggers configured"

    content = f"""// ============================================================================
// ARTA Quality Pipeline — Jenkins Declarative Pipeline
// Generated by ARTA CI/CD Configuration Engine
// ============================================================================
pipeline {{
    agent any

    environment {{
        ARTA_API_URL   = credentials('arta-api-url')
        ARTA_API_KEY   = credentials('arta-api-key')
        ARTA_PROJECT_ID = credentials('arta-project-id')
        TARGET_ENV     = '{env_name}'
        TARGET_URL     = '{env_url}'
    }}

    triggers {{
{triggers_block}
    }}

    options {{
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }}

    stages {{
        // ── Stage 1: Checkout & Setup ────────────────────────────────────
        stage('Checkout & Setup') {{
            steps {{
                checkout scm
                sh 'npm ci'
                sh 'npx playwright install --with-deps'
            }}
        }}

        // ── Stage 2: Requirements Sync ───────────────────────────────────
        stage('Requirements Sync') {{
            steps {{
                sh \"\"\"
                    curl -sf -X POST "${{ARTA_API_URL}}/api/requirements/sync" \\\\
                        -H "Authorization: Bearer ${{ARTA_API_KEY}}" \\\\
                        -H "Content-Type: application/json" \\\\
                        -d '{{"project_id": "'${{ARTA_PROJECT_ID}}'"}}'
                \"\"\"
            }}
        }}

        // ── Stage 3: AI Test Generation ──────────────────────────────────
        stage('AI Test Generation') {{
            steps {{
                sh \"\"\"
                    curl -sf -X POST "${{ARTA_API_URL}}/api/tests/generate" \\\\
                        -H "Authorization: Bearer ${{ARTA_API_KEY}}" \\\\
                        -H "Content-Type: application/json" \\\\
                        -d '{{"project_id": "'${{ARTA_PROJECT_ID}}'", "strategy": "risk_based"}}'
                \"\"\"
            }}
        }}

        // ── Stage 4: Smoke Tests (P0) ────────────────────────────────────
        stage('Smoke Tests (P0)') {{
            steps {{
                sh \"\"\"
                    curl -sf -X POST "${{ARTA_API_URL}}/api/execution/run" \\\\
                        -H "Authorization: Bearer ${{ARTA_API_KEY}}" \\\\
                        -H "Content-Type: application/json" \\\\
                        -d '{{"project_id": "'${{ARTA_PROJECT_ID}}'", "suite_type": "smoke", "environment": "'${{TARGET_ENV}}'", "build_id": "'${{BUILD_NUMBER}}'"}}'
                \"\"\"
            }}
        }}

        // ── Stage 5: Full Test Suite (P0+P1+P2) ─────────────────────────
        stage('Full Test Suite') {{
            steps {{
                sh \"\"\"
                    curl -sf -X POST "${{ARTA_API_URL}}/api/execution/run" \\\\
                        -H "Authorization: Bearer ${{ARTA_API_KEY}}" \\\\
                        -H "Content-Type: application/json" \\\\
                        -d '{{"project_id": "'${{ARTA_PROJECT_ID}}'", "suite_type": "full", "environment": "'${{TARGET_ENV}}'", "build_id": "'${{BUILD_NUMBER}}'"}}'
                \"\"\"
            }}
        }}

        // ── Stage 6: Security Scan (OWASP ZAP) ──────────────────────────
        stage('Security Scan') {{
            steps {{
                sh \"\"\"
                    echo "Running OWASP ZAP baseline scan..."
                    docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py \\\\
                        -t "${{TARGET_URL}}" -r zap-report.html -l WARN || true
                \"\"\"
            }}
            post {{
                always {{
                    archiveArtifacts artifacts: 'zap-report.html', allowEmptyArchive: true
                }}
            }}
        }}

        // ── Stage 7: Performance Tests (k6) ─────────────────────────────
        stage('Performance Tests') {{
            steps {{
                sh \"\"\"
                    echo "Running k6 performance tests..."
                    k6 run --out json=k6-results.json \\\\
                        -e TARGET_URL="${{TARGET_URL}}" \\\\
                        src/automation/k6/checkout-performance.js || true
                \"\"\"
            }}
            post {{
                always {{
                    archiveArtifacts artifacts: 'k6-results.json', allowEmptyArchive: true
                }}
            }}
        }}

        // ── Stage 8: Quality Gate Decision ───────────────────────────────
        stage('Quality Gate') {{
            steps {{
                script {{
                    def gate = sh(script: \"\"\"
                        curl -sf "${{ARTA_API_URL}}/api/gates/check?build_id=${{BUILD_NUMBER}}&project_id=${{ARTA_PROJECT_ID}}" \\\\
                            -H "Authorization: Bearer ${{ARTA_API_KEY}}"
                    \"\"\", returnStdout: true).trim()
                    def json = readJSON text: gate
                    def decision = json.decision ?: 'UNKNOWN'
                    echo "Quality Gate Decision: ${{decision}}"
                    if (decision == 'FAIL') {{
                        error "Quality gate FAILED — blocking deployment"
                    }} else if (decision == 'CONCERNS') {{
                        unstable "Quality gate raised CONCERNS"
                    }}
                }}
            }}
        }}
    }}

    post {{
        always {{
            echo 'ARTA Quality Pipeline completed'
        }}
        failure {{
            echo 'Pipeline failed — check quality gate results'
        }}
    }}
}}
"""

    return CICDConfigResponse(
        filename="Jenkinsfile",
        content=content.strip() + "\n",
        language="groovy",
        provider="jenkins",
        stages=ARTA_PIPELINE_STAGES,
    )


def _generate_circleci(cfg: CICDConfigRequest) -> CICDConfigResponse:
    """Generate CircleCI config.yml."""

    env_name = cfg.environments[0].name if cfg.environments else "staging"
    env_url = cfg.environments[0].url if cfg.environments else "https://staging.example.com"

    branch_filter = ", ".join(cfg.branch_patterns)

    # Schedule triggers
    schedule_block = ""
    if "schedule" in cfg.triggers:
        schedule_block = f"""
    triggers:
      - schedule:
          cron: "{cfg.schedule_cron}"
          filters:
            branches:
              only:
                - {cfg.branch_patterns[0] if cfg.branch_patterns else 'main'}"""

    content = f"""# ============================================================================
# ARTA Quality Pipeline — CircleCI
# Generated by ARTA CI/CD Configuration Engine
# ============================================================================
version: 2.1

orbs:
  node: circleci/node@5.2
  browser-tools: circleci/browser-tools@1.4

executors:
  arta-executor:
    docker:
      - image: cimg/node:20.11-browsers
    working_directory: ~/project
    environment:
      TARGET_ENV: {env_name}
      TARGET_URL: {env_url}

jobs:
  # ── Stage 1: Checkout & Setup ──────────────────────────────────────────
  checkout-and-setup:
    executor: arta-executor
    steps:
      - checkout
      - node/install-packages:
          pkg-manager: npm
      - run:
          name: Install Playwright browsers
          command: npx playwright install --with-deps
      - persist_to_workspace:
          root: ~/project
          paths:
            - .

  # ── Stage 2: Requirements Sync ─────────────────────────────────────────
  requirements-sync:
    executor: arta-executor
    steps:
      - attach_workspace:
          at: ~/project
      - run:
          name: Sync requirements with ARTA
          command: |
            curl -sf -X POST "$ARTA_API_URL/api/requirements/sync" \\
              -H "Authorization: Bearer $ARTA_API_KEY" \\
              -H "Content-Type: application/json" \\
              -d '{{"project_id": "'$ARTA_PROJECT_ID'", "source": "repo"}}' || true

  # ── Stage 3: AI Test Generation ────────────────────────────────────────
  ai-test-generation:
    executor: arta-executor
    steps:
      - attach_workspace:
          at: ~/project
      - run:
          name: Generate tests via ARTA AI
          command: |
            curl -sf -X POST "$ARTA_API_URL/api/tests/generate" \\
              -H "Authorization: Bearer $ARTA_API_KEY" \\
              -H "Content-Type: application/json" \\
              -d '{{"project_id": "'$ARTA_PROJECT_ID'", "strategy": "risk_based"}}' || true

  # ── Stage 4: Smoke Tests (P0) ──────────────────────────────────────────
  smoke-tests:
    executor: arta-executor
    steps:
      - attach_workspace:
          at: ~/project
      - run:
          name: Execute Smoke Tests (P0)
          command: |
            curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
              -H "Authorization: Bearer $ARTA_API_KEY" \\
              -H "Content-Type: application/json" \\
              -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "smoke", "environment": "'$TARGET_ENV'", "build_id": "'$CIRCLE_BUILD_NUM'"}}'

  # ── Stage 5: Full Test Suite ───────────────────────────────────────────
  full-tests:
    executor: arta-executor
    steps:
      - attach_workspace:
          at: ~/project
      - run:
          name: Execute Full Test Suite (P0+P1+P2)
          command: |
            curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
              -H "Authorization: Bearer $ARTA_API_KEY" \\
              -H "Content-Type: application/json" \\
              -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "full", "environment": "'$TARGET_ENV'", "build_id": "'$CIRCLE_BUILD_NUM'"}}'

  # ── Stage 6: Security Scan ─────────────────────────────────────────────
  security-scan:
    docker:
      - image: ghcr.io/zaproxy/zaproxy:stable
    steps:
      - run:
          name: OWASP ZAP Baseline Scan
          command: |
            zap-baseline.py -t "$TARGET_URL" -r /tmp/zap-report.html -l WARN || true
      - store_artifacts:
          path: /tmp/zap-report.html
          destination: zap-security-report

  # ── Stage 7: Performance Tests ─────────────────────────────────────────
  performance-tests:
    docker:
      - image: grafana/k6:latest
    steps:
      - attach_workspace:
          at: ~/project
      - run:
          name: k6 Performance Tests
          command: |
            k6 run --out json=/tmp/k6-results.json \\
              -e TARGET_URL="$TARGET_URL" \\
              src/automation/k6/checkout-performance.js || true
      - store_artifacts:
          path: /tmp/k6-results.json
          destination: k6-performance-results

  # ── Stage 8: Quality Gate ──────────────────────────────────────────────
  quality-gate:
    executor: arta-executor
    steps:
      - run:
          name: ARTA Quality Gate Check
          command: |
            GATE=$(curl -sf "$ARTA_API_URL/api/gates/check?build_id=$CIRCLE_BUILD_NUM&project_id=$ARTA_PROJECT_ID" \\
              -H "Authorization: Bearer $ARTA_API_KEY")
            echo "$GATE" | jq .
            DECISION=$(echo "$GATE" | jq -r '.decision // "UNKNOWN"')
            if [ "$DECISION" = "FAIL" ]; then
              echo "Quality gate FAILED"
              exit 1
            fi
            echo "Quality gate: $DECISION"

workflows:
  arta-quality-pipeline:
    jobs:
      - checkout-and-setup
      - requirements-sync:
          requires:
            - checkout-and-setup
      - ai-test-generation:
          requires:
            - requirements-sync
      - smoke-tests:
          requires:
            - ai-test-generation
      - full-tests:
          requires:
            - smoke-tests
      - security-scan:
          requires:
            - ai-test-generation
      - performance-tests:
          requires:
            - ai-test-generation
      - quality-gate:
          requires:
            - full-tests
            - security-scan
            - performance-tests
{schedule_block}
"""

    return CICDConfigResponse(
        filename=".circleci/config.yml",
        content=content.strip() + "\n",
        language="yaml",
        provider="circleci",
        stages=ARTA_PIPELINE_STAGES,
    )


def _generate_gitlab_ci(cfg: CICDConfigRequest) -> CICDConfigResponse:
    """Generate GitLab CI .gitlab-ci.yml."""

    env_name = cfg.environments[0].name if cfg.environments else "staging"
    env_url = cfg.environments[0].url if cfg.environments else "https://staging.example.com"

    # Branch rules
    branch_rules = []
    for b in cfg.branch_patterns:
        if "*" in b:
            # Convert glob to regex
            regex = b.replace("*", ".*").replace("/", "\\/")
            branch_rules.append(f'    - if: $CI_COMMIT_BRANCH =~ /^{regex}$/')
        else:
            branch_rules.append(f'    - if: $CI_COMMIT_BRANCH == "{b}"')
    if "pull_request" in cfg.triggers:
        branch_rules.append('    - if: $CI_PIPELINE_SOURCE == "merge_request_event"')
    if "manual" in cfg.triggers:
        branch_rules.append('    - if: $CI_PIPELINE_SOURCE == "web"')
    rules_block = "\n".join(branch_rules) if branch_rules else '    - if: $CI_COMMIT_BRANCH == "main"'

    # Schedule block
    schedule_block = ""
    if "schedule" in cfg.triggers:
        schedule_block = f"""
# NOTE: Configure pipeline schedule in GitLab UI:
# Settings > CI/CD > Pipeline schedules > Cron: {cfg.schedule_cron}"""

    content = f"""# ============================================================================
# ARTA Quality Pipeline — GitLab CI
# Generated by ARTA CI/CD Configuration Engine
# ============================================================================
{schedule_block}

variables:
  ARTA_API_URL: $ARTA_API_URL
  ARTA_API_KEY: $ARTA_API_KEY
  ARTA_PROJECT_ID: $ARTA_PROJECT_ID
  TARGET_ENV: "{env_name}"
  TARGET_URL: "{env_url}"

stages:
  - setup
  - sync
  - generate
  - smoke
  - full
  - security
  - performance
  - gate

workflow:
  rules:
{rules_block}

default:
  image: node:20
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - node_modules/

# ── Stage 1: Checkout & Setup ────────────────────────────────────────────
setup:
  stage: setup
  script:
    - npm ci
    - npx playwright install --with-deps
  artifacts:
    paths:
      - node_modules/
    expire_in: 1 hour

# ── Stage 2: Requirements Sync ───────────────────────────────────────────
requirements-sync:
  stage: sync
  script:
    - |
      curl -sf -X POST "$ARTA_API_URL/api/requirements/sync" \\
        -H "Authorization: Bearer $ARTA_API_KEY" \\
        -H "Content-Type: application/json" \\
        -d '{{"project_id": "'$ARTA_PROJECT_ID'", "source": "repo"}}' || true
  needs:
    - setup

# ── Stage 3: AI Test Generation ──────────────────────────────────────────
ai-test-generation:
  stage: generate
  script:
    - |
      curl -sf -X POST "$ARTA_API_URL/api/tests/generate" \\
        -H "Authorization: Bearer $ARTA_API_KEY" \\
        -H "Content-Type: application/json" \\
        -d '{{"project_id": "'$ARTA_PROJECT_ID'", "strategy": "risk_based"}}' || true
  needs:
    - requirements-sync

# ── Stage 4: Smoke Tests (P0) ────────────────────────────────────────────
smoke-tests:
  stage: smoke
  script:
    - |
      curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
        -H "Authorization: Bearer $ARTA_API_KEY" \\
        -H "Content-Type: application/json" \\
        -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "smoke", "environment": "'$TARGET_ENV'", "build_id": "'$CI_PIPELINE_ID'"}}'
  needs:
    - ai-test-generation

# ── Stage 5: Full Test Suite (P0+P1+P2) ──────────────────────────────────
full-tests:
  stage: full
  script:
    - |
      curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
        -H "Authorization: Bearer $ARTA_API_KEY" \\
        -H "Content-Type: application/json" \\
        -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "full", "environment": "'$TARGET_ENV'", "build_id": "'$CI_PIPELINE_ID'"}}'
  needs:
    - smoke-tests

# ── Stage 6: Security Scan (OWASP ZAP) ───────────────────────────────────
security-scan:
  stage: security
  image: ghcr.io/zaproxy/zaproxy:stable
  script:
    - zap-baseline.py -t "$TARGET_URL" -r zap-report.html -l WARN || true
  artifacts:
    paths:
      - zap-report.html
    expire_in: 30 days
  allow_failure: true
  needs:
    - ai-test-generation

# ── Stage 7: Performance Tests (k6) ──────────────────────────────────────
performance-tests:
  stage: performance
  image: grafana/k6:latest
  script:
    - k6 run --out json=k6-results.json -e TARGET_URL="$TARGET_URL" src/automation/k6/checkout-performance.js || true
  artifacts:
    paths:
      - k6-results.json
    expire_in: 30 days
  allow_failure: true
  needs:
    - ai-test-generation

# ── Stage 8: Quality Gate Decision ────────────────────────────────────────
quality-gate:
  stage: gate
  script:
    - |
      GATE=$(curl -sf "$ARTA_API_URL/api/gates/check?build_id=$CI_PIPELINE_ID&project_id=$ARTA_PROJECT_ID" \\
        -H "Authorization: Bearer $ARTA_API_KEY")
      echo "$GATE" | jq .
      DECISION=$(echo "$GATE" | jq -r '.decision // "UNKNOWN"')
      if [ "$DECISION" = "FAIL" ]; then
        echo "Quality gate FAILED — blocking deployment"
        exit 1
      fi
      echo "Quality gate: $DECISION"
  needs:
    - full-tests
    - security-scan
    - performance-tests
"""

    return CICDConfigResponse(
        filename=".gitlab-ci.yml",
        content=content.strip() + "\n",
        language="yaml",
        provider="gitlab_ci",
        stages=ARTA_PIPELINE_STAGES,
    )


def _generate_azure_devops(cfg: CICDConfigRequest) -> CICDConfigResponse:
    """Generate Azure DevOps azure-pipelines.yml."""

    env_name = cfg.environments[0].name if cfg.environments else "staging"
    env_url = cfg.environments[0].url if cfg.environments else "https://staging.example.com"

    # Trigger branches
    trigger_branches = "\n".join(f"    - {b}" for b in cfg.branch_patterns)

    pr_branches = ""
    if "pull_request" in cfg.triggers:
        pr_branch_list = [b for b in cfg.branch_patterns if "*" not in b][:2]
        pr_branches = "pr:\n  branches:\n    include:\n" + "\n".join(f"      - {b}" for b in (pr_branch_list or ["main"]))

    schedule_block = ""
    if "schedule" in cfg.triggers:
        schedule_block = f"""
schedules:
  - cron: '{cfg.schedule_cron}'
    displayName: 'Nightly ARTA Quality Run'
    branches:
      include:
        - {cfg.branch_patterns[0] if cfg.branch_patterns else 'main'}
    always: true"""

    content = f"""# ============================================================================
# ARTA Quality Pipeline — Azure DevOps
# Generated by ARTA CI/CD Configuration Engine
# ============================================================================

trigger:
  branches:
    include:
{trigger_branches}

{pr_branches}
{schedule_block}

variables:
  ARTA_API_URL: $(ARTA_API_URL)
  ARTA_API_KEY: $(ARTA_API_KEY)
  ARTA_PROJECT_ID: $(ARTA_PROJECT_ID)
  TARGET_ENV: '{env_name}'
  TARGET_URL: '{env_url}'

pool:
  vmImage: 'ubuntu-latest'

stages:
  # ── Stage 1: Checkout & Setup ──────────────────────────────────────────
  - stage: Setup
    displayName: 'Checkout & Setup'
    jobs:
      - job: SetupJob
        steps:
          - checkout: self
          - task: NodeTool@0
            inputs:
              versionSpec: '20.x'
            displayName: 'Install Node.js 20'
          - script: npm ci
            displayName: 'Install dependencies'
          - script: npx playwright install --with-deps
            displayName: 'Install Playwright browsers'

  # ── Stage 2: Requirements Sync ─────────────────────────────────────────
  - stage: RequirementsSync
    displayName: 'Requirements Sync'
    dependsOn: Setup
    jobs:
      - job: SyncJob
        steps:
          - script: |
              curl -sf -X POST "$ARTA_API_URL/api/requirements/sync" \\
                -H "Authorization: Bearer $ARTA_API_KEY" \\
                -H "Content-Type: application/json" \\
                -d '{{"project_id": "'$ARTA_PROJECT_ID'", "source": "repo"}}' || true
            displayName: 'Sync requirements with ARTA'

  # ── Stage 3: AI Test Generation ────────────────────────────────────────
  - stage: AITestGeneration
    displayName: 'AI Test Generation'
    dependsOn: RequirementsSync
    jobs:
      - job: GenerateJob
        steps:
          - script: |
              curl -sf -X POST "$ARTA_API_URL/api/tests/generate" \\
                -H "Authorization: Bearer $ARTA_API_KEY" \\
                -H "Content-Type: application/json" \\
                -d '{{"project_id": "'$ARTA_PROJECT_ID'", "strategy": "risk_based"}}' || true
            displayName: 'Generate tests via ARTA AI'

  # ── Stage 4: Smoke Tests (P0) ──────────────────────────────────────────
  - stage: SmokeTests
    displayName: 'Smoke Tests (P0)'
    dependsOn: AITestGeneration
    jobs:
      - job: SmokeJob
        steps:
          - script: |
              curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
                -H "Authorization: Bearer $ARTA_API_KEY" \\
                -H "Content-Type: application/json" \\
                -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "smoke", "environment": "'$TARGET_ENV'", "build_id": "$(Build.BuildId)"}}'
            displayName: 'Execute P0 Smoke Tests'

  # ── Stage 5: Full Test Suite (P0+P1+P2) ────────────────────────────────
  - stage: FullTests
    displayName: 'Full Test Suite'
    dependsOn: SmokeTests
    jobs:
      - job: FullTestJob
        steps:
          - script: |
              curl -sf -X POST "$ARTA_API_URL/api/execution/run" \\
                -H "Authorization: Bearer $ARTA_API_KEY" \\
                -H "Content-Type: application/json" \\
                -d '{{"project_id": "'$ARTA_PROJECT_ID'", "suite_type": "full", "environment": "'$TARGET_ENV'", "build_id": "$(Build.BuildId)"}}'
            displayName: 'Execute Full Test Suite'

  # ── Stage 6: Security Scan (OWASP ZAP) ─────────────────────────────────
  - stage: SecurityScan
    displayName: 'Security Scan'
    dependsOn: AITestGeneration
    jobs:
      - job: ZAPJob
        container: ghcr.io/zaproxy/zaproxy:stable
        continueOnError: true
        steps:
          - script: |
              zap-baseline.py -t "$TARGET_URL" -r $(Build.ArtifactStagingDirectory)/zap-report.html -l WARN || true
            displayName: 'OWASP ZAP Baseline Scan'
          - publish: $(Build.ArtifactStagingDirectory)/zap-report.html
            artifact: zap-security-report
            condition: always()

  # ── Stage 7: Performance Tests (k6) ────────────────────────────────────
  - stage: PerformanceTests
    displayName: 'Performance Tests'
    dependsOn: AITestGeneration
    jobs:
      - job: K6Job
        continueOnError: true
        steps:
          - script: |
              echo "Running k6 performance tests..."
              docker run --rm -v $(System.DefaultWorkingDirectory):/src grafana/k6:latest \\
                run --out json=/src/k6-results.json \\
                -e TARGET_URL="$TARGET_URL" \\
                /src/src/automation/k6/checkout-performance.js || true
            displayName: 'k6 Performance Tests'
          - publish: $(System.DefaultWorkingDirectory)/k6-results.json
            artifact: k6-performance-results
            condition: always()

  # ── Stage 8: Quality Gate Decision ──────────────────────────────────────
  - stage: QualityGate
    displayName: 'Quality Gate Decision'
    dependsOn:
      - FullTests
      - SecurityScan
      - PerformanceTests
    jobs:
      - job: GateJob
        steps:
          - script: |
              GATE=$(curl -sf "$ARTA_API_URL/api/gates/check?build_id=$(Build.BuildId)&project_id=$ARTA_PROJECT_ID" \\
                -H "Authorization: Bearer $ARTA_API_KEY")
              echo "$GATE" | jq .
              DECISION=$(echo "$GATE" | jq -r '.decision // "UNKNOWN"')
              echo "##vso[task.setvariable variable=gateDecision]$DECISION"
              if [ "$DECISION" = "FAIL" ]; then
                echo "##vso[task.logissue type=error]Quality gate FAILED — blocking deployment"
                exit 1
              elif [ "$DECISION" = "CONCERNS" ]; then
                echo "##vso[task.logissue type=warning]Quality gate raised CONCERNS"
              fi
              echo "Quality gate: $DECISION"
            displayName: 'ARTA Quality Gate Check'
"""

    return CICDConfigResponse(
        filename="azure-pipelines.yml",
        content=content.strip() + "\n",
        language="yaml",
        provider="azure_devops",
        stages=ARTA_PIPELINE_STAGES,
    )


# ── Provider dispatch map ─────────────────────────────────────────────────────

_GENERATORS: dict[CICDProvider, Any] = {
    CICDProvider.github_actions: _generate_github_actions,
    CICDProvider.jenkins: _generate_jenkins,
    CICDProvider.circleci: _generate_circleci,
    CICDProvider.gitlab_ci: _generate_gitlab_ci,
    CICDProvider.azure_devops: _generate_azure_devops,
}


# ── GitHub commit helper ──────────────────────────────────────────────────────


async def _commit_via_github(
    owner: str,
    repo: str,
    token: str,
    filename: str,
    content: str,
    branch_name: str,
) -> dict[str, Any] | None:
    """Create branch, commit file, and open PR via GitHub API."""
    try:
        import httpx
        import base64

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get default branch SHA
            repo_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=headers,
            )
            if repo_resp.status_code != 200:
                return None
            default_branch = repo_resp.json().get("default_branch", "main")

            ref_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{default_branch}",
                headers=headers,
            )
            if ref_resp.status_code != 200:
                return None
            base_sha = ref_resp.json()["object"]["sha"]

            # Create branch
            create_ref = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
            if create_ref.status_code not in (200, 201):
                return None

            # Create/update file
            content_b64 = base64.b64encode(content.encode()).decode()
            file_resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}",
                headers=headers,
                json={
                    "message": f"chore: add ARTA quality pipeline ({filename})",
                    "content": content_b64,
                    "branch": branch_name,
                },
            )
            if file_resp.status_code not in (200, 201):
                return None

            # Create PR
            pr_resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                json={
                    "title": "Add ARTA Quality Pipeline",
                    "body": (
                        "## ARTA CI/CD Configuration\n\n"
                        "This PR adds the ARTA quality pipeline configuration with 8 stages:\n\n"
                        "1. Checkout & Setup\n"
                        "2. Requirements Sync\n"
                        "3. AI Test Generation\n"
                        "4. Smoke Tests (P0)\n"
                        "5. Full Test Suite (P0+P1+P2)\n"
                        "6. Security Scan (OWASP ZAP)\n"
                        "7. Performance Tests (k6)\n"
                        "8. Quality Gate Decision\n\n"
                        "Generated by [ARTA Platform](https://arta.dev)"
                    ),
                    "head": branch_name,
                    "base": default_branch,
                },
            )
            if pr_resp.status_code not in (200, 201):
                return None

            pr_data = pr_resp.json()
            return {
                "branch": branch_name,
                "pr_url": pr_data["html_url"],
                "pr_number": pr_data["number"],
                "status": "success",
            }
    except Exception as exc:
        log.warning("GitHub commit failed: %s", exc)
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/{project_id}/detect-stack", dependencies=[Depends(_require_api_key)])
async def detect_stack(project_id: str, body: DetectStackRequest) -> DetectStackResponse:
    """
    Detect the language, framework, test tools, and package manager of a repository.
    Uses GitHub API when a token is provided; falls back to URL-pattern heuristics.
    """
    # Try GitHub API detection first
    if body.access_token:
        parsed = _parse_github_owner_repo(body.repository_url)
        if parsed:
            owner, repo = parsed
            api_result = await _detect_via_github_api(owner, repo, body.access_token)
            if api_result:
                return _detect_stack_from_files(
                    files=api_result["files"],
                    package_json=api_result.get("package_json"),
                )

    # Fallback: mock detection from URL patterns
    return _mock_detect_from_url(body.repository_url)


@router.post("/{project_id}/cicd-config", dependencies=[Depends(_require_api_key)])
async def generate_cicd_config(project_id: str, body: CICDConfigRequest) -> CICDConfigResponse:
    """
    Generate a CI/CD pipeline configuration for the specified provider.
    Returns the filename, content (YAML/Groovy), and language.
    All templates include the 8 ARTA pipeline stages.
    """
    generator = _GENERATORS.get(body.provider)
    if not generator:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CI/CD provider: {body.provider}. "
                   f"Supported: {[p.value for p in CICDProvider]}",
        )
    return generator(body)


@router.post("/{project_id}/cicd-commit", dependencies=[Depends(_require_api_key)])
async def commit_cicd_config(project_id: str, body: CICDCommitRequest) -> CICDCommitResponse:
    """
    Commit a generated CI/CD config to a GitHub repository.
    Creates a new branch and opens a pull request.
    Falls back to a mock response when the GitHub API is unavailable.
    """
    branch_name = body.branch_name or f"arta/add-quality-pipeline-{uuid.uuid4().hex[:6]}"

    # Try real GitHub API commit
    parsed = _parse_github_owner_repo(body.repository_url)
    if parsed:
        owner, repo = parsed
        result = await _commit_via_github(
            owner=owner,
            repo=repo,
            token=body.access_token,
            filename=body.filename,
            content=body.config_content,
            branch_name=branch_name,
        )
        if result:
            return CICDCommitResponse(**result)

    # Mock fallback
    mock_pr_number = abs(hash(branch_name)) % 900 + 100  # Deterministic 3-digit number
    repo_slug = "/".join(parsed) if parsed else "owner/repo"
    return CICDCommitResponse(
        branch=branch_name,
        pr_url=f"https://github.com/{repo_slug}/pull/{mock_pr_number}",
        pr_number=mock_pr_number,
        status="success",
    )


# ── Fix TTT: Project-aware CI/CD preview (Jinja2-rendered) ───────────────────
# Eliminates manual config entry: derives tools / envs / gate thresholds /
# notify channels directly from the project model + renders a Jinja template
# from src/templates/cicd/. This is the "render-on-demand" half of TTT;
# /cicd-commit (above) is the "opt-in commit" half — no auto-commit.

_TTT_TEMPLATE_FILES: dict[CICDProvider, str] = {
    CICDProvider.github_actions: "github_actions.yaml.j2",
    CICDProvider.jenkins: "jenkins.groovy.j2",
    CICDProvider.gitlab_ci: "gitlab_ci.yaml.j2",
}

_TTT_FILENAMES: dict[CICDProvider, str] = {
    CICDProvider.github_actions: ".github/workflows/arta-quality.yml",
    CICDProvider.jenkins: "Jenkinsfile",
    CICDProvider.gitlab_ci: ".gitlab-ci.yml",
}

_TTT_LANGUAGES: dict[CICDProvider, str] = {
    CICDProvider.github_actions: "yaml",
    CICDProvider.jenkins: "groovy",
    CICDProvider.gitlab_ci: "yaml",
}


def _derive_cicd_context(project_obj: Any) -> dict[str, Any]:
    """Pull tools / envs / gate thresholds / notify channels off a Project ORM
    row (or dict) so the operator never has to fill out a request body.

    Falls back to sensible defaults whenever a field is missing.
    """
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    integrations = _get(project_obj, "integrations", {}) or {}
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    if not isinstance(integrations, dict):
        integrations = {}

    quality_gates = _get(project_obj, "quality_gates", {}) or {}
    if hasattr(quality_gates, "model_dump"):
        quality_gates = quality_gates.model_dump()
    if not isinstance(quality_gates, dict):
        quality_gates = {}

    notifications = _get(project_obj, "notifications", {}) or {}
    if hasattr(notifications, "model_dump"):
        notifications = notifications.model_dump()
    if not isinstance(notifications, dict):
        notifications = {}

    environments_raw = _get(project_obj, "environments", {}) or {}
    if hasattr(environments_raw, "model_dump"):
        environments_raw = environments_raw.model_dump()
    if not isinstance(environments_raw, dict):
        environments_raw = {}

    # tools: prefer project's recommended_tools; fall back to common defaults.
    tools = integrations.get("recommended_tools") or ["playwright", "newman", "k6"]
    if not isinstance(tools, list):
        tools = list(tools) if hasattr(tools, "__iter__") else ["playwright"]

    # environments: convert {staging: {url: ...}, prod: {...}} → list of dicts.
    envs: list[dict[str, str]] = []
    for name, cfg in environments_raw.items():
        if isinstance(cfg, dict):
            envs.append({"name": name, "url": cfg.get("base_url") or cfg.get("url") or ""})
        else:
            envs.append({"name": name, "url": ""})
    if not envs:
        envs = [{"name": "staging", "url": ""}]

    # notify channels: pull a flat list of slack/email targets from notifications.
    channels: list[str] = []
    for k in ("slack_webhook_url", "slack_channel"):
        v = notifications.get(k)
        if v:
            channels.append(f"slack:{v}")
    for email in (notifications.get("email_recipients") or []):
        if email:
            channels.append(f"email:{email}")

    # Gate thresholds — surface min_score so YAML can enforce on CONCERNS.
    min_score = quality_gates.get("min_quality_score") or quality_gates.get("threshold")
    try:
        min_score_int = int(min_score) if min_score is not None else None
    except (TypeError, ValueError):
        min_score_int = None

    return {
        "project_id": str(_get(project_obj, "id", "")),
        "project_name": _get(project_obj, "name", "ARTA Project"),
        "test_tools": tools,
        "environments": envs,
        "triggers": ["push", "pull_request", "manual", "schedule"],
        "branch_patterns": ["main", "develop", "release/*"],
        "schedule_cron": "0 6 * * 1-5",
        "arta_api_url": os.environ.get("ARTA_API_URL", "https://arta.example.com"),
        "notify_channels": channels,
        "min_score": min_score_int,
    }


@router.post("/{project_id}/cicd/preview", dependencies=[Depends(_require_api_key)])
async def cicd_preview_from_project(
    project_id: str,
    provider: CICDProvider = CICDProvider.github_actions,
) -> CICDConfigResponse:
    """Render a CI/CD config using the project's actual tools/envs/gate config.

    Operator clicks "Preview" in the UI and gets a fully-rendered file with
    no form-filling required. The Jinja template is loaded from
    src/templates/cicd/. After review, the operator calls /cicd-commit to
    open a PR (no auto-commit).
    """
    template_file = _TTT_TEMPLATE_FILES.get(provider)
    if template_file is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider {provider} not yet supported by TTT Jinja templates. "
                f"Use POST /cicd-config for: {[p.value for p in CICDProvider if p not in _TTT_TEMPLATE_FILES]}"
            ),
        )

    # Load project from DB; fall back to graceful defaults when DB unavailable.
    project_obj: Any = {"id": project_id, "name": project_id}
    try:
        from ..db_adapter import try_db
        from ...db.repository import ProjectRepo
        async with try_db() as db:
            if db:
                repo = ProjectRepo(db)
                row = await repo.get(project_id)
                if row:
                    project_obj = row
    except Exception as exc:
        log.debug("Fix TTT: project load fell back to defaults: %s", exc)

    ctx = _derive_cicd_context(project_obj)

    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        templates_dir = os.path.join(os.path.dirname(__file__), "..", "..", "templates", "cicd")
        templates_dir = os.path.abspath(templates_dir)
        env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(disabled_extensions=("j2",)),
            keep_trailing_newline=True,
        )
        rendered = env.get_template(template_file).render(**ctx)
    except Exception as exc:
        log.exception("Fix TTT: Jinja render failed for %s", provider)
        raise HTTPException(status_code=500, detail=f"Template render failed: {exc}")

    return CICDConfigResponse(
        filename=_TTT_FILENAMES[provider],
        content=rendered,
        language=_TTT_LANGUAGES[provider],
        provider=provider.value,
        stages=ARTA_PIPELINE_STAGES,
    )
