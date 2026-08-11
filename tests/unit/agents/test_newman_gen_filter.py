"""R77.1.γ — regression tests for `_filter_endpoints_by_gherkin`.

The OpenAPI subset embedded in the Newman LLM prompt was previously the
entire L14-narrowed list (often 100+ lines). For a typical Gherkin
scenario only ~5-15 endpoints are actually relevant; the rest waste
LLM context and dilute attention.

R77.1.γ scores each endpoint line by word-overlap with the Gherkin text
and keeps the top-N. Fallbacks: when the endpoint list is short, the
Gherkin has too few distinguishing keywords, or every kept line scored
zero → return the original list.
"""
from __future__ import annotations

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Happy path: relevance-scored filtering ──────────────────────────────

def test_filter_keeps_relevant_endpoints_first():
    """Endpoints whose words overlap with the Gherkin must rank first."""
    api_endpoints = "\n".join([
        f"GET /api/orders/{{id}}/items   {desc}"
        for desc in ["unrelated description"] * 60
    ] + [
        "POST /api/checkout/submit  Submit checkout order with payment processing",
        "GET /api/checkout/status   Read checkout status of a pending order",
    ])
    gherkin = (
        "Scenario: User completes checkout submission\n"
        "  Given the user has items in the cart\n"
        "  When they submit the checkout payment\n"
        "  Then the order status reflects pending\n"
    )
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        api_endpoints, gherkin, top_n=20,
    )
    lines = filtered.splitlines()
    # Both checkout-related endpoints must survive (they share many tokens
    # with the Gherkin: checkout, submit, payment, order, status, pending).
    assert any("checkout/submit" in ln for ln in lines)
    assert any("checkout/status" in ln for ln in lines)


def test_filter_reduces_count_to_top_n():
    """When the list exceeds top_n, the output must shrink to exactly top_n."""
    api_endpoints = "\n".join(
        f"GET /api/users/{i}/profile description {i}"
        for i in range(200)
    )
    gherkin = (
        "Scenario: Get user profile\n"
        "  Given a registered user\n"
        "  When the profile API is called\n"
        "  Then we receive user details\n"
    )
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        api_endpoints, gherkin, top_n=50,
    )
    assert len(filtered.splitlines()) == 50


# ── Fallbacks ──────────────────────────────────────────────────────────

def test_short_endpoint_list_unchanged():
    """If the list already ≤ top_n, the filter should pass through."""
    raw = "GET /api/x\nPOST /api/y\nGET /api/z"
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        raw, "any gherkin text here", top_n=50,
    )
    assert filtered == raw


def test_empty_endpoint_list_unchanged():
    """Empty input round-trips."""
    assert (
        AutomationEngineerAgent._filter_endpoints_by_gherkin(
            "", "some gherkin", top_n=50,
        )
        == ""
    )


def test_short_gherkin_falls_back_to_full_list():
    """If the Gherkin has fewer than 3 distinguishing keywords (4+ chars),
    the keyword filter can't discriminate — return original."""
    api_endpoints = "\n".join(
        f"GET /api/r{i} description" for i in range(100)
    )
    short_gherkin = "Do it now."   # No 4-char tokens worth scoring
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        api_endpoints, short_gherkin, top_n=20,
    )
    assert filtered == api_endpoints


def test_zero_overlap_falls_back_to_full_list():
    """If the top-N candidates all scored 0 (no Gherkin word matches
    any endpoint), the filter has no information to discriminate —
    return original rather than ship random top-N."""
    api_endpoints = "\n".join(
        f"GET /xxxxxx{i}/yyyyy zzzzz"   # All unique nonsense tokens
        for i in range(100)
    )
    gherkin = "Scenario: Login the user account session credentials"
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        api_endpoints, gherkin, top_n=20,
    )
    assert filtered == api_endpoints


# ── Stability ─────────────────────────────────────────────────────────

def test_ties_preserve_original_order():
    """When two lines have the same score, the earlier one in the input
    should come first in the output (idx tiebreaker)."""
    api_endpoints = "\n".join([
        # Both contain "order" — but the first one appears earlier.
        "GET /api/orders/first   describes order placement",
        "GET /api/orders/second  describes order placement",
    ] + [f"GET /api/x/{i}/y  unrelated" for i in range(60)])
    gherkin = "Scenario: place order describes placement"
    filtered = AutomationEngineerAgent._filter_endpoints_by_gherkin(
        api_endpoints, gherkin, top_n=10,
    )
    lines = filtered.splitlines()
    first_idx = next(i for i, ln in enumerate(lines) if "/first" in ln)
    second_idx = next(i for i, ln in enumerate(lines) if "/second" in ln)
    assert first_idx < second_idx
