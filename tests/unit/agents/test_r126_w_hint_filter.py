"""R126.W — selector/endpoint hint top-N filter for the R126.B scaffolder.

The R126.B PW skeleton embeds a small "available selectors" + "available
endpoints" hint inside each test() block's LLM_FILL placeholder. The
quality of the LLM-generated test body depends entirely on whether those
hints are RELEVANT to the AC's Gherkin scenario.

R126.W reuses R77.1.γ's Gherkin-keyword overlap scoring (single source
of truth for relevance ranking) and adapts it for two input shapes:
- DOM catalog role+name tuples → returned to R126.B as `getByRole(...)` candidates
- Captured endpoint dicts → returned as `page.request.X(url, ...)` candidates
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Selector picking ──

def test_r126w_selector_filter_picks_keyword_match():
    """Catalog entries whose name overlaps Gherkin keywords rank top."""
    catalog = {
        "role_names": [
            ("button", "Login"),
            ("button", "Submit"),
            ("button", "Cancel"),
            ("link", "Dashboard"),
            ("link", "Settings"),
            ("link", "Profile"),
            ("textbox", "Email"),
            ("textbox", "Password"),
        ],
    }
    ac_gherkin = "Scenario: User logs in\n  When the user enters Email and Password\n  Then the dashboard appears"
    top = AutomationEngineerAgent._r126_w_pick_hint_selectors(ac_gherkin, catalog, top_n=4)
    # Email/Password/Dashboard should all rank above Cancel/Profile/Settings
    top_names = {n for _, n in top}
    assert "Email" in top_names
    assert "Password" in top_names
    assert "Dashboard" in top_names


def test_r126w_selector_filter_empty_catalog_returns_empty():
    top = AutomationEngineerAgent._r126_w_pick_hint_selectors("scenario", {}, top_n=15)
    assert top == []
    top = AutomationEngineerAgent._r126_w_pick_hint_selectors("scenario", None, top_n=15)
    assert top == []


def test_r126w_selector_filter_smaller_than_top_n_returns_all():
    """When catalog has ≤top_n entries, return all (no scoring needed)."""
    catalog = {"role_names": [("button", "Foo"), ("button", "Bar")]}
    top = AutomationEngineerAgent._r126_w_pick_hint_selectors("any gherkin", catalog, top_n=15)
    assert len(top) == 2


def test_r126w_selector_filter_falls_back_when_no_keyword_match():
    """When Gherkin yields no keyword overlap, return first-N (catalog ordering)."""
    catalog = {
        "role_names": [
            ("button", "Alpha"), ("button", "Beta"), ("button", "Gamma"),
            ("button", "Delta"), ("button", "Epsilon"),
        ],
    }
    # Gherkin has no overlap with any name → top_scores == {0} → fallback
    top = AutomationEngineerAgent._r126_w_pick_hint_selectors(
        "Scenario: completely unrelated text", catalog, top_n=2,
    )
    assert len(top) == 2
    # Fallback returns first-N in catalog order
    assert top[0] == ("button", "Alpha")


# ── Endpoint picking ──

def test_r126w_endpoint_filter_picks_keyword_match():
    """Captured endpoints whose path overlaps Gherkin keywords rank top."""
    captured = [
        {"method": "POST", "path": "/api/v1/login"},
        {"method": "GET", "path": "/api/v1/dashboard/widgets"},
        {"method": "GET", "path": "/api/v1/billing/invoices"},
        {"method": "POST", "path": "/api/v1/payments/checkout"},
        {"method": "GET", "path": "/api/v1/profile/settings"},
    ]
    ac_gherkin = "Scenario: User logs in\n  When the user submits Login\n  Then dashboard widgets load"
    top = AutomationEngineerAgent._r126_w_pick_hint_endpoints(ac_gherkin, captured, top_n=3)
    paths = {ep["path"] for ep in top}
    assert "/api/v1/login" in paths
    assert "/api/v1/dashboard/widgets" in paths


def test_r126w_endpoint_filter_empty_captured_returns_empty():
    assert AutomationEngineerAgent._r126_w_pick_hint_endpoints("any", [], top_n=8) == []
    assert AutomationEngineerAgent._r126_w_pick_hint_endpoints("any", None, top_n=8) == []


def test_r126w_endpoint_filter_top_n_respected():
    """Result is capped at top_n."""
    captured = [{"method": "GET", "path": f"/api/v1/x{i}"} for i in range(20)]
    top = AutomationEngineerAgent._r126_w_pick_hint_endpoints("scenario", captured, top_n=5)
    assert len(top) == 5


def test_r126w_endpoint_filter_skips_malformed_entries():
    """Non-dict / missing-path entries are skipped."""
    captured = [
        {"method": "GET", "path": "/api/v1/valid"},
        None,  # malformed
        {"method": "POST"},  # missing path
        "string-not-dict",  # malformed
        {"method": "GET", "path": ""},  # empty path
    ]
    top = AutomationEngineerAgent._r126_w_pick_hint_endpoints("scenario", captured, top_n=8)
    assert len(top) == 1
    assert top[0]["path"] == "/api/v1/valid"
