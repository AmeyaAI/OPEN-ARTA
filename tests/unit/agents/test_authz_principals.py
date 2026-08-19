"""authz_principals — principal-archetype fixtures (RBAC oracle subjects)."""
import json
from pathlib import Path

from src.agents import authz_principals as P

CATALOG = {"role_permissions": {
    "organizations-viewer": ["org.members.read", "org.projects.read"],
    "organizations-admin": ["org.members.read", "org.members.remove"],
    "iam-admin": ["iam.groups.read", "iam.groups.create"],
}}

PRINCIPALS = [
    {"id": "U20", "label": "org-viewer @ org=testorg", "login": "user1@testorg.example",
     "principal_type": "customer", "home_org": "testorg",
     "bindings": [{"role": "organizations-viewer", "scope": "org", "target": "testorg"}]},
    {"id": "U3", "label": "iam-admin @ platform", "login": "iam-admin1@vendor.example",
     "principal_type": "operator", "home_org": "vendor",
     "bindings": [{"role": "iam-admin", "scope": "platform", "target": ""}]},
]


def test_validation_flags_structural_and_catalog_issues():
    bad = PRINCIPALS + [
        {"id": "U20", "login": "dup@x", "principal_type": "customer",   # duplicate id
         "bindings": [{"role": "organizations-viewer", "scope": "org", "target": "t"}]},
        {"id": "BAD", "login": "", "principal_type": "customer",        # missing login
         "bindings": [{"role": "no-such-role", "scope": "org", "target": "t"}]},  # bad role
    ]
    v = P.validate_principals(bad, CATALOG)
    assert not v["valid"]
    joined = " ".join(v["issues"])
    assert "duplicate id" in joined
    assert "missing login" in joined
    assert "not in permission catalog" in joined


def test_clean_fixtures_validate():
    v = P.validate_principals(PRINCIPALS, CATALOG)
    assert v["valid"] and v["issue_count"] == 0


def test_effective_permissions_keyed_by_scope_target():
    eff = P.resolve_effective_permissions(PRINCIPALS[0], CATALOG)
    assert "org.members.read" in eff["org|testorg"]      # customer, org-scoped
    eff_admin = P.resolve_effective_permissions(PRINCIPALS[1], CATALOG)
    assert "iam.groups.create" in eff_admin["platform|"]  # operator, platform (no org)


def test_service_account_needs_no_bindings():
    sa = [{"id": "SA1", "login": "svc-token", "principal_type": "service_account",
           "bindings": []}]
    assert P.validate_principals(sa, CATALOG)["valid"]


def test_persist_load_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_PRINCIPALS_DIR", Path(tmp_path))
    P.persist_principals("pid-1", PRINCIPALS)
    assert len(P.load_principals("pid-1")) == 2
    s = P.summarize_principals("pid-1", CATALOG)
    assert s["loaded"] and s["principal_count"] == 2 and s["valid"]
    assert s["by_type"] == {"customer": 1, "operator": 1}
    assert "vendor" in s["home_orgs"] and "testorg" in s["home_orgs"]


def test_missing_fixtures_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_PRINCIPALS_DIR", Path(tmp_path))
    assert P.load_principals("no-such") is None
    assert P.summarize_principals("no-such")["loaded"] is False


def test_killswitch(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_PRINCIPALS_DIR", Path(tmp_path))
    P.persist_principals("pid-2", PRINCIPALS)
    monkeypatch.setenv("ARTA_AUTHZ_PRINCIPALS_DISABLE", "1")
    assert P.load_principals("pid-2") is None
