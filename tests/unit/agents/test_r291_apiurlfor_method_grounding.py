"""R291 — endpoint+method grounding must see through the `apiUrlFor(...)` wrapper.

`_PW_API_REQUEST_RE` required the URL to be a quoted string IMMEDIATELY after the
request paren — `request.post('/x')`. But R207.B/R210 rewrote happy-path requests
to `request.post(apiUrlFor('/x'))` so each family gets the right token. That
`apiUrlFor(` broke the match, so endpoint AND method grounding were SILENTLY
SKIPPED for the modern form.

Live cost (run-771720, faithful exec of the 4 specs): a spec POSTing a GET-only
endpoint (`GetAccountRelationshipHierarchy`) passed grounding and 405'd at
runtime; fabricated `apiUrlFor` paths passed too. Both are grounding gaps that
should have been caught at gen time and steered by the R57.1 retry hint.
"""
from src.agents.grounding_validator import (
    _PW_API_REQUEST_RE,
    validate_playwright_grounded,
)

_HDR = ("import { test, expect } from '@playwright/test';\n"
        "import { apiUrlFor, authHeaderFor } from '../common/arta_auth';\n"
        "// AC: AC-1\n")


def _spec(verb: str, path: str) -> str:
    return (_HDR + "test('a', async ({ request }) => {\n"
            f"  const r = await request.{verb}(apiUrlFor('{path}'), {{ headers: authHeaderFor('/x') }});\n"
            "});\n")


def _endpoint_flags(v):
    return [x for x in (v or []) if "endpoint" in x.kind.lower()]


# ── the regex now sees the apiUrlFor form ───────────────────────────────────

def test_regex_matches_apiurlfor_wrapper():
    line = "await request.post(apiUrlFor('/AccountManagement/api/X/GetY'));"
    ms = list(_PW_API_REQUEST_RE.finditer(line))
    assert len(ms) == 1
    assert ms[0].group(1).lower() == "post"
    assert ms[0].group(2) == "/AccountManagement/api/X/GetY"


def test_regex_still_matches_the_direct_string_form():
    """R291 must not break the pre-existing direct-URL form."""
    line = "await request.get('/AccountManagement/api/X/GetY');"
    ms = list(_PW_API_REQUEST_RE.finditer(line))
    assert len(ms) == 1 and ms[0].group(1).lower() == "get"


def test_regex_matches_page_request_apiurlfor():
    line = "await page.request.post(apiUrlFor('/Reefer/api/getData'));"
    ms = list(_PW_API_REQUEST_RE.finditer(line))
    assert len(ms) == 1 and ms[0].group(1).lower() == "post"


# ── method grounding through apiUrlFor ──────────────────────────────────────

_CAPS_GET = [{"method": "GET",
              "path": "/AccountManagement/api/AccountRelationShip/GetAccountRelationshipHierarchy",
              "source": "requirement"}]


def test_post_to_a_get_only_endpoint_is_flagged():
    v = validate_playwright_grounded(
        _spec("post", "/AccountManagement/api/AccountRelationShip/GetAccountRelationshipHierarchy"),
        project_id="p", dom_catalog={"routes": {}}, captured_endpoints=_CAPS_GET)
    assert _endpoint_flags(v), "POST to a GET-only endpoint must flag (it 405s at runtime)"


def test_correct_method_passes():
    v = validate_playwright_grounded(
        _spec("get", "/AccountManagement/api/AccountRelationShip/GetAccountRelationshipHierarchy"),
        project_id="p", dom_catalog={"routes": {}}, captured_endpoints=_CAPS_GET)
    assert not _endpoint_flags(v), "the correct GET must pass"


def test_correct_post_to_a_post_endpoint_passes():
    caps = [{"method": "POST", "path": "/Reefer/api/getDataForCommandCenter",
             "source": "requirement"}]
    v = validate_playwright_grounded(
        _spec("post", "/Reefer/api/getDataForCommandCenter"),
        project_id="p", dom_catalog={"routes": {}}, captured_endpoints=caps)
    assert not _endpoint_flags(v), "a real POST read must not be a false positive"


def test_fabricated_apiurlfor_path_is_flagged():
    """The other half of the same gap: a hallucinated path in apiUrlFor was
    invisible too."""
    v = validate_playwright_grounded(
        _spec("get", "/AccountManagement/api/Totally/Fabricated/Path"),
        project_id="p", dom_catalog={"routes": {}}, captured_endpoints=_CAPS_GET)
    assert _endpoint_flags(v)
