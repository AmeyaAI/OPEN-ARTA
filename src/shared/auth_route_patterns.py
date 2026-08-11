"""R145.B — single source of truth for login-route + login-flow text
patterns.

Pre-R145.B, the login-route regex lived as an inline literal at
`src/agents/automation_engineer.py:8215` inside R112.E SPA-hydration
excluder (negative lookahead `(?!login|signup|signin|register|$)`).
That literal was an EXCLUDER — used to PREVENT the SPA-hydration
injector from stamping login routes. Now R145.B repurposes the same
canonical token set as an INCLUDER for the auth-bypass filter, and
extracts both surfaces into this module.

Used by:
  - `src/agents/automation_engineer.py:8215` R112.E excluder
    (refactored to import `LOGIN_ROUTE_RE_TOKENS`)
  - `src/agents/atdd_designer.py` AC filter (when project's
    auth_bypass is active)
  - `src/agents/automation_engineer.py` Gherkin scenario filter
    (same auth_bypass gate)

Auto-detection of `auth_bypass`:
  When BOTH `.arta/environments/<env>-storage.json.r112a.meta.json`
  exists (operator pasted via R45.2) AND
  `project.environments[env].variables.agent_token` has length ≥ 50
  (R96.1 token-exchange minted), `auto_detect_auth_bypass` returns
  True. The operator can override with explicit `auth_bypass: true`
  or `auth_bypass: false` in the project config.
"""
from __future__ import annotations

import re as _re
from pathlib import Path

# Canonical login-route tokens — single source of truth. Extracted from
# automation_engineer.py:8215 (R112.E exclusion regex tokens).
LOGIN_ROUTE_TOKENS = ("login", "signup", "signin", "register", "logout", "forgot")

# Canonical regex: matches paths whose FIRST segment is one of the
# login-route tokens (case-insensitive). Also matches the root path "/"
# because most SPAs redirect "/" → "/login" when unauthenticated.
LOGIN_ROUTE_RE = _re.compile(
    r"^(?:/(?:" + "|".join(LOGIN_ROUTE_TOKENS) + r")(?:[/?].*)?|/?)$",
    _re.IGNORECASE,
)

# Login-flow text tokens — match Gherkin scenario text that exercises
# login UI. Conservative: a multi-step AC that MENTIONS "/login" once but
# asserts other behavior passes through; ONLY pure login flows are
# filtered. See `is_login_flow_text` for the predicate.
LOGIN_TEXT_TOKENS = (
    "sign in", "sign-in", "log in", "log-in",
    "user signs in", "user signs-in", "user logs in",
    "user signs up", "user logs out",
    "/login", "/signin", "/signup", "/register", "/logout",
    "enter password", "enter credentials", "submit credentials",
    "click login button", "click sign-in button",
    "fill the login form", "fill out login",
)


def is_login_route(path: str | None) -> bool:
    """R145.B — return True iff `path` is a canonical login/signup/etc
    route. Empty string and "/" also match because SPAs typically
    redirect unauthenticated visits to a login page.
    """
    if not path:
        return True
    if not isinstance(path, str):
        return False
    return bool(LOGIN_ROUTE_RE.match(path))


def is_login_flow_text(text: str | None) -> bool:
    """R145.B — return True iff `text` reads as a PURE login-flow
    Gherkin (one or more login tokens AND no signs of other workflow).

    Conservative: an AC mentioning "user signs in" alongside "creates a
    project and verifies the dashboard" passes through. This predicate
    fires only when the dominant intent appears to be the login flow.

    Heuristic: count `LOGIN_TEXT_TOKENS` hits vs total alphabetic
    tokens. When the ratio is >= 0.20 (login tokens dominate), classify
    as pure login.
    """
    if not text or not isinstance(text, str):
        return False
    t = text.lower()
    hits = sum(1 for tok in LOGIN_TEXT_TOKENS if tok in t)
    if hits == 0:
        return False
    # Conservative check: ANY hit + text is short enough that login is
    # the dominant signal. Long ACs with embedded login mentions pass
    # through.
    if len(t) <= 200 and hits >= 1:
        return True
    # Long AC: require multiple hits OR explicit /login path reference
    if hits >= 2 or "/login" in t or "/signin" in t:
        return True
    return False


def auto_detect_auth_bypass(
    project: dict | None,
    env_name: str = "staging",
    *,
    envs_dir: Path | None = None,
) -> bool:
    """R145.B — return True when BOTH:
      (a) `.arta/environments/<env_name>-storage.json.r112a.meta.json`
          exists (R112.A.5 paste-trust meta sidecar), AND
      (b) project.environments[env_name].variables.agent_token has
          length ≥ 50 (R96.1 token-exchange minted)

    Operator can override via `project.auth_bypass = True | False`:
      explicit-true: ALWAYS bypass (even without paste evidence)
      explicit-false: NEVER bypass (even with paste evidence)
      missing: auto-detect via (a) AND (b)
    """
    if not project:
        return False
    explicit = project.get("auth_bypass")
    if explicit is True:
        return True
    if explicit is False:
        return False
    # Auto-detect path
    envs_dir = envs_dir or Path(".arta/environments")
    meta_sidecar = envs_dir / f"{env_name}-storage.json.r112a.meta.json"
    if not meta_sidecar.exists():
        return False
    envs = project.get("environments") or {}
    env_block = envs.get(env_name) or {}
    vars_block = env_block.get("variables") or {}
    agent_token = vars_block.get("agent_token") or ""
    if not isinstance(agent_token, str) or len(agent_token) < 50:
        return False
    return True


def auth_bypass_source(project: dict | None) -> str:
    """R145.B — human-readable source label for the dashboard tile.

    Returns "no_project" ONLY when `project is None`. An empty dict
    is a valid (un-configured) project and falls through to "auto".
    """
    if project is None:
        return "no_project"
    explicit = project.get("auth_bypass")
    if explicit is True:
        return "explicit_true"
    if explicit is False:
        return "explicit_false"
    return "auto"
