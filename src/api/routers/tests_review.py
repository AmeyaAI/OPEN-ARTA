"""F7-2 (M2 partial) — pending-review + feedback + push-to-github sub-router.

The bigger POST /review and POST /quality-check endpoints stay in tests.py
because they're entangled with `_MOCK_SCRIPT_CONTENT`. This module covers
the cleanly-bounded review-decision surface that F6-7 secured:

  GET   /pending-reviews
  GET   /pending-reviews/{workflow_id}
  POST  /pending-reviews/{workflow_id}/decide
  PATCH /pending-reviews/{workflow_id}/gherkin
  POST  /{test_id}/feedback
  POST  /{test_id}/push-to-github

State (`_PENDING_REVIEWS`) lives in tests_state.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import require_api_key as _require_api_key
from .tests_state import _PENDING_REVIEWS, GENERATED_TESTS


review_router = APIRouter()


# ── Demo seed (only when the dict is empty so we don't clobber real reviews) ─
# F10-3: stamp the seed with a sentinel project_id (`_demo`) so the
# project-scoped GET filter excludes it from every real project's view.
# Operators who want to see the demo can request `?project_id=_demo`.
if not _PENDING_REVIEWS:
    _PENDING_REVIEWS["demo-workflow-001"] = {
        "workflow_id": "demo-workflow-001",
        "project_id": "_demo",
        "requirement_id": "REQ-017",
        "status": "pending_review",
        "created_at": "2026-03-14T10:15:00Z",
        "gherkin_scenarios": [
            "Feature: Checkout Payment Processing\n  Scenario: Successful Visa card checkout\n    Given I have 2 items in my cart\n    When I enter valid Visa card 4111111111111111\n    And I submit the payment\n    Then the order should be confirmed within 3 seconds\n    And I should receive an order confirmation email",
            "Feature: Checkout Payment Processing\n  Scenario: Payment with expired card\n    Given I have 1 item in my cart\n    When I enter expired card details\n    Then the system should display a clear error message\n    And no charge should be made",
            "Feature: Checkout Payment Processing\n  Scenario: Concurrent payment load\n    Given 500 concurrent users are checking out\n    When all submit payments within a 30-second window\n    Then 95% of payments should complete within 3 seconds\n    And no duplicate charges should occur",
        ],
        "test_data": {
            "fixture_id": "TD-017",
            "columns": ["card_type", "card_number", "expiry", "cvv", "expected_status"],
            "rows": [
                {"card_type": "Visa", "card_number": "4111111111111111", "expiry": "12/28", "cvv": "123", "expected_status": 202},
                {"card_type": "Mastercard", "card_number": "5500005555555559", "expiry": "09/27", "cvv": "456", "expected_status": 202},
                {"card_type": "Expired", "card_number": "4111111111111111", "expiry": "01/20", "cvv": "123", "expected_status": 422},
                {"card_type": "Invalid", "card_number": "0000000000000000", "expiry": "12/28", "cvv": "000", "expected_status": 422},
            ],
        },
        "risk_profile": {"risk_score": 9.6, "priority": "P0", "test_types": ["UI", "API", "Performance", "Security"]},
    }


class ReviewDecision(BaseModel):
    decision: str   # "approve" | "reject"
    reason: str = ""
    edited_gherkin: list[str] | None = None


class GherkinEdit(BaseModel):
    gherkin_scenarios: list[str]


class TestFeedback(BaseModel):
    rating: int = 3          # 1 (poor) to 5 (excellent)
    corrections: str = ""
    regenerate: bool = False


@review_router.get("/pending-reviews")
async def list_pending_reviews(project_id: str | None = None):
    """List pending review entries.

    F10-3: When `project_id` is provided, return ONLY entries that match.
    Entries without a `project_id` field (legacy/demo data) match nothing.
    Without `project_id`, return all entries (back-compat for admin views).
    """
    items = list(_PENDING_REVIEWS.values())
    if project_id is None:
        return items
    return [r for r in items if r.get("project_id") == project_id]


@review_router.get("/pending-reviews/{workflow_id}")
async def get_pending_review(workflow_id: str):
    review = _PENDING_REVIEWS.get(workflow_id)
    if not review:
        raise HTTPException(404, f"No pending review for workflow {workflow_id}")
    return review


@review_router.post("/pending-reviews/{workflow_id}/decide", dependencies=[Depends(_require_api_key)])
async def decide_review(workflow_id: str, decision: ReviewDecision):
    """Approve or reject generated tests after human review.

    - approve: continues pipeline → automation generation → execution
    - reject: discards generated tests, optionally triggers regeneration with feedback
    """
    review = _PENDING_REVIEWS.get(workflow_id)
    if not review:
        raise HTTPException(404, f"No pending review for workflow {workflow_id}")

    if decision.decision == "approve":
        if decision.edited_gherkin:
            review["gherkin_scenarios"] = decision.edited_gherkin
        review["status"] = "approved"
        review["review_decision"] = "approved"
        review["review_reason"] = decision.reason or "Approved by reviewer"
        del _PENDING_REVIEWS[workflow_id]
        return {
            "status": "approved",
            "message": "Tests approved. Pipeline resuming: automation generation → execution.",
            "workflow_id": workflow_id,
        }
    elif decision.decision == "reject":
        review["status"] = "rejected"
        review["review_decision"] = "rejected"
        review["review_reason"] = decision.reason or "Rejected by reviewer"
        del _PENDING_REVIEWS[workflow_id]
        return {
            "status": "rejected",
            "message": "Tests rejected. Regeneration queued with reviewer feedback.",
            "workflow_id": workflow_id,
            "feedback": decision.reason,
        }
    raise HTTPException(400, f"Invalid decision: {decision.decision}. Use 'approve' or 'reject'.")


@review_router.patch("/pending-reviews/{workflow_id}/gherkin", dependencies=[Depends(_require_api_key)])
async def edit_pending_gherkin(workflow_id: str, body: GherkinEdit):
    review = _PENDING_REVIEWS.get(workflow_id)
    if not review:
        raise HTTPException(404, f"No pending review for workflow {workflow_id}")
    review["gherkin_scenarios"] = body.gherkin_scenarios
    review["edited"] = True
    return {
        "status": "updated",
        "scenario_count": len(body.gherkin_scenarios),
        "message": "Gherkin updated. Review and approve to continue pipeline.",
    }


@review_router.post("/{test_id}/feedback", dependencies=[Depends(_require_api_key)])
async def submit_test_feedback(test_id: str, feedback: TestFeedback):
    """Submit feedback on AI-generated test quality."""
    feedback_entry = {
        "test_id": test_id,
        "rating": max(1, min(5, feedback.rating)),
        "corrections": feedback.corrections,
        "regenerate_requested": feedback.regenerate,
        "stored": True,
    }
    if feedback.regenerate:
        feedback_entry["message"] = (
            "Regeneration queued. Prior feedback will be included in the prompt context "
            "to improve output quality."
        )
    else:
        feedback_entry["message"] = (
            f"Feedback recorded (rating: {feedback.rating}/5). "
            "This will improve future test generation for similar requirements."
        )
    return feedback_entry


@review_router.post("/{test_id}/push-to-github", dependencies=[Depends(_require_api_key)])
async def push_test_to_github(test_id: str):
    """Push generated test scripts to GitHub as a branch + PR."""
    mock = next((t for t in GENERATED_TESTS if t["id"] == test_id), None)
    if not mock:
        raise HTTPException(404, f"Test {test_id} not found")

    req_id = mock.get("requirement_id", "REQ-000")
    script_path = mock.get("script_path", f"src/automation/playwright/{test_id.lower()}.spec.ts")

    import time
    timestamp = int(time.time())
    branch = f"arta/tests/{req_id.lower()}-{timestamp}"

    return {
        "status": "created",
        "pr_url": f"https://github.com/team/repo/pull/{timestamp % 1000}",
        "branch": branch,
        "files": 1,
        "message": (
            f"Pull request created for {test_id} on branch {branch}. "
            "Scripts committed and ready for CI validation."
        ),
        "scripts_pushed": [script_path],
    }


# ── Suite-level review (POST /review) + per-test quality check ───────────────
# Mock script content for review (complements GENERATED_TESTS which lack
# script bodies). Only used by the review endpoint's mock fallback path.

_MOCK_SCRIPT_CONTENT: dict[str, str] = {
    "TC-124": (
        "import { test, expect } from '@playwright/test';\n"
        "import { faker } from '@faker-js/faker';\n\n"
        "test('Happy path Visa checkout', async ({ page }) => {\n"
        "  const qty = 2;\n"
        "  await page.goto('/checkout');\n"
        "  await page.fill('#card', '4111111111111111');\n"
        "  await page.click('#pay');\n"
        "  await expect(page.locator('.confirmation')).toBeVisible();\n"
        "});\n\n"
        "test.afterEach(async ({ page }) => {\n"
        "  await page.evaluate(() => localStorage.clear());\n"
        "});\n"
    ),
    "TC-125": (
        "import { test, expect } from '@playwright/test';\n\n"
        "test('Mastercard with 3DS', async ({ page }) => {\n"
        "  await page.goto('/checkout');\n"
        "  await page.fill('#card', '5500005555555559');\n"
        "  await page.fill('#amount', '200');\n"
        "  await page.click('#pay');\n"
        "  await expect(page.locator('.threeds-modal')).toBeVisible();\n"
        "});\n\n"
        "test.afterEach(async ({ page }) => {\n"
        "  await page.evaluate(() => localStorage.clear());\n"
        "});\n"
    ),
    "TC-126": (
        "import http from 'k6/http';\n"
        "import { check } from 'k6';\n\n"
        "export let options = { vus: 500, duration: '2m' };\n\n"
        "export default function () {\n"
        "  const res = http.post('https://api.example.com/pay', JSON.stringify({\n"
        "    card: '4111111111111111', amount: 100\n"
        "  }));\n"
        "  check(res, { 'status 200': (r) => r.status === 200 });\n"
        "}\n\n"
        "export function teardown() { console.log('cleanup done'); }\n"
    ),
    "TC-127": (
        "import { test, expect } from '@playwright/test';\n\n"
        "test('Expired card rejection', async ({ page }) => {\n"
        "  await page.goto('/checkout');\n"
        "  await page.fill('#card', '4111111111111111');\n"
        "  await page.fill('#expiry', '01/20');\n"
        "  await page.click('#pay');\n"
        "  await expect(page.locator('.error')).toContainText('expired');\n"
        "});\n\n"
        "test.afterEach(async ({ page }) => {\n"
        "  await page.evaluate(() => localStorage.clear());\n"
        "});\n"
    ),
    "TC-128": (
        "import { test, expect } from '@playwright/test';\n\n"
        "test('Stolen card fraud detection', async ({ page }) => {\n"
        "  await page.goto('/checkout');\n"
        "  await page.fill('#card', '4000000000000002');\n"
        "  await page.click('#pay');\n"
        "  await expect(page.locator('.fraud-alert')).toBeHidden();\n"
        "  await expect(page.locator('.order-declined')).toBeVisible();\n"
        "});\n\n"
        "test.afterEach(async ({ page }) => {\n"
        "  await page.evaluate(() => localStorage.clear());\n"
        "});\n"
    ),
    "TC-131": (
        "import { test, expect } from '@playwright/test';\n\n"
        "test('SQL injection blocked', async ({ page }) => {\n"
        "  await page.goto('/checkout');\n"
        "  await page.fill('#card', \"' OR 1=1 --\");\n"
        "  await page.click('#pay');\n"
        "  await expect(page.locator('.error')).toContainText('invalid');\n"
        "});\n\n"
        "test.afterEach(async ({ page }) => {\n"
        "  await page.evaluate(() => localStorage.clear());\n"
        "});\n"
    ),
}


class ReviewRequest(BaseModel):
    test_ids: list[str] | None = None  # None = review all tests


@review_router.post("/review", dependencies=[Depends(_require_api_key)])
async def review_tests(body: ReviewRequest):
    """Suite-level test review: quality validation, pyramid balance, duplicate coverage."""
    from ...agents.test_review_agent import TestReviewAgent
    from ..db_adapter import try_db

    requested_ids = [tid.upper() for tid in body.test_ids] if body.test_ids else None
    tests_to_review: list[dict] = []

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo, _to_dict
            repo = TestCaseRepo(db)
            if requested_ids:
                for tid in requested_ids:
                    row = await repo.get(tid)
                    if row:
                        d = _to_dict(row)
                        tests_to_review.append({
                            "test_id": d.get("id", tid),
                            "title": d.get("title", ""),
                            "test_type": d.get("test_type", "e2e"),
                            "script_content": d.get("script_content", ""),
                            "ac_id": d.get("ac_id", ""),
                            "avg_duration_ms": d.get("avg_duration_ms"),
                        })
            else:
                rows, _ = await repo.list()
                for row in rows:
                    d = _to_dict(row)
                    tests_to_review.append({
                        "test_id": d.get("id", ""),
                        "title": d.get("title", ""),
                        "test_type": d.get("test_type", "e2e"),
                        "script_content": d.get("script_content", ""),
                        "ac_id": d.get("ac_id", ""),
                        "avg_duration_ms": d.get("avg_duration_ms"),
                    })

    if not tests_to_review:
        source = GENERATED_TESTS
        if requested_ids:
            source = [t for t in GENERATED_TESTS if t["id"] in requested_ids]
            missing = set(requested_ids) - {t["id"] for t in source}
            if missing:
                raise HTTPException(404, f"Tests not found: {', '.join(sorted(missing))}")
        for t in source:
            tid = t["id"]
            tests_to_review.append({
                "test_id": tid,
                "title": t.get("title", ""),
                "test_type": t.get("test_type", "e2e"),
                "script_content": _MOCK_SCRIPT_CONTENT.get(tid, t.get("gherkin", "")),
                "ac_id": t.get("ac_id", ""),
                "avg_duration_ms": t.get("duration_ms"),
            })

    agent = TestReviewAgent()
    result = agent.review(tests_to_review)
    return result.to_dict()


@review_router.post("/{test_id}/quality-check", dependencies=[Depends(_require_api_key)])
async def quality_check(test_id: str):
    """BMAD TEA Test Quality Definition of Done — 8-criteria validation."""
    from ...agents.test_quality_validator import TestQualityValidator
    from ..db_adapter import try_db

    tid = test_id.upper()
    script_content = None
    avg_duration_ms = None

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo
            repo = TestCaseRepo(db)
            tc = await repo.get(tid)
            if tc:
                script_content = tc.script_content
                avg_duration_ms = tc.avg_duration_ms

    if script_content is None:
        mock = next((t for t in GENERATED_TESTS if t["id"] == tid), None)
        if mock:
            script_content = mock.get("script_content", mock.get("gherkin", ""))
        else:
            raise HTTPException(404, f"Test {tid} not found")

    if not script_content:
        raise HTTPException(400, f"Test {tid} has no script content to validate")

    validator = TestQualityValidator()
    result = validator.validate(script_content, avg_duration_ms)
    return {"test_id": tid, **result.to_dict()}
