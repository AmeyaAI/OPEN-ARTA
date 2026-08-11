"""B1 — `_generate_accessibility` must wire the real-route + DOM-catalog
grounding into the axe prompt (so axe navigates real authenticated routes, not
'/'), reusing the PW route-grounding helpers. Source-asserted."""
from __future__ import annotations

import inspect

from src.agents.automation_engineer import AutomationEngineerAgent as AE


def test_generate_accessibility_wires_route_grounding():
    src = inspect.getsource(AE._generate_accessibility)
    assert "_r202_target_routes" in src
    assert "_r180_app_flow_nav_constraint" in src
    assert "format_dom_catalog_for_prompt" in src
    assert "GROUNDED ROUTES" in src
    assert "ARTA_AXE_GROUNDING_DISABLE" in src


def test_template_has_auth_and_spa_ready():
    from src.prompts.tea_prompts import ACCESSIBILITY_GENERATION as T
    assert "../common/sub_flows" in T
    assert "skipIfAuthStale" in T and "waitForSPAReady" in T
    assert "not.toHaveURL" in T
    assert "GROUNDED ROUTES" in T
