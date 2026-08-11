"""A2 — axe must NOT vacuous-PASS. `_r_axe_reached_real_page` distinguishes a
real scan (clean or violations) from the SPA login/selection wall, an
all-skipped run, or a no-scan — so a "0 violations" on an un-scanned page
BLOCKs (a11y not assessed) instead of falsely reading clean.
"""
from __future__ import annotations

from src.api.routers.execution import _r_axe_reached_real_page


def test_real_scan_clean_is_reached():
    # specs executed checkA11y, 0 violations → reached + clean (a real PASS)
    reached, reason = _r_axe_reached_real_page("3 passed (12s)", "", [])
    assert reached is True and reason == ""


def test_real_scan_with_violations_is_reached():
    # a failed a11y test = it scanned + found violations → reached
    reached, _ = _r_axe_reached_real_page("2 passed\n1 failed", "",
                                          [{"impact": "serious", "id": "color-contrast"}])
    assert reached is True


def test_auth_stale_token_not_reached():
    for blob in ("Test skipped: auth_stale_url_redirect", "skipIfAuthStale fired",
                 "Redirected to /login", "AUTH STATE STALE"):
        reached, reason = _r_axe_reached_real_page(blob, "", [])
        assert reached is False and reason == "auth_stale", blob


def test_all_skipped_empty_report_not_reached():
    # nothing executed checkA11y + empty report → cannot confirm a real scan
    reached, reason = _r_axe_reached_real_page("0 passed\n3 skipped (1s)", "", [])
    assert reached is False and reason == "all_skipped_or_no_scan"


def test_violations_present_overrides_zero_executed():
    # report has entries (a scan happened) even if the summary parse is empty
    reached, _ = _r_axe_reached_real_page("", "", [{"impact": "moderate", "id": "label"}])
    assert reached is True
