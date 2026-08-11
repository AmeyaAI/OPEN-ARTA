"""ARTA Requirements Router — CRUD + LLM-powered parsing."""
from __future__ import annotations

import logging
import os
import json

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from .auth import require_role

log = logging.getLogger("arta")


import os

def _load_requirements():
    global PROJECT_REQUIREMENTS
    os.makedirs('.arta', exist_ok=True)
    try:
        with open('.arta/requirements.json', 'r') as f:
            raw = json.load(f)
        # Validate structure: must be {project_id: [list_of_dicts]}
        # Discard any entries that aren't project_id → list mappings
        PROJECT_REQUIREMENTS = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, list):
                    PROJECT_REQUIREMENTS[k] = v
    except (FileNotFoundError, json.JSONDecodeError):
        PROJECT_REQUIREMENTS = {}

    # Ensure persistence file exists
    if not os.path.exists('.arta/requirements.json'):
        _save_requirements()

def _save_requirements():
    try:
        from ...telemetry import bucket as _tel_bucket, emit as _tel_emit
        _tel_emit("requirements.imported",
                  {"count_bucket": _tel_bucket(sum(len(v) for v in PROJECT_REQUIREMENTS.values()))})
    except Exception:
        pass
    try:
        with open('.arta/requirements.json', 'w') as f:
            json.dump(PROJECT_REQUIREMENTS, f)
            f.flush()
    except (PermissionError, OSError) as e:
        log.warning("Could not persist requirements to disk: %s", e)

_load_requirements()

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


class ParseRequest(BaseModel):
    text: str
    source_type: str = "user_story"


class RequirementCreate(BaseModel):
    title: str
    description: str
    source_type: str = "user_story"
    constraints: list[str] = []
    regulations: list[str] = []


# ── Per-project requirement storage (in-memory) ──────────────────────────────
# Keys are project_id strings. The demo E-Commerce project uses a well-known ID.
PROJECT_REQUIREMENTS: dict[str, list[dict]] = {}

# ── Seed requirements for BugTrackr ──
BUGTRACKR_SEED_REQUIREMENTS = {
    "bug_crud": {
        "title": "Bug CRUD Operations",
        "description": "Create, read, update, delete bugs with validation",
        "constraints": ["data validation", "error handling"],
        "regulations": ["ISO 9001:2015"]
    },
    "status_workflow": {
        "title": "Status Workflow",
        "description": "Bug status transitions: Open → In Progress → Resolved → Closed",
        "constraints": ["state machine pattern", "audit logging"]
    },
    "rbac": {
        "title": "Role-Based Access Control",
        "description": "Admin/Developer/Tester roles with permissions",
        "constraints": ["least privilege", "session security"]
    },
    "comments": {
        "title": "Comment System",
        "description": "Threaded comments on bugs",
        "constraints": ["real-time sync", "notification hooks"]
    },
    "activity_logs": {
        "title": "Activity Tracking",
        "description": "Audit trail of all bug operations",
        "constraints": ["immutable logs", "retention policy"]
    },
    "dashboard": {
        "title": "Project Dashboard",
        "description": "Bug statistics and filters",
        "constraints": ["real-time updates", "data visualization"]
    },
    "health_api": {
        "title": "Health Check API",
        "description": "Database and service status monitoring",
        "constraints": ["HTTP 200 OK", "response time < 200ms"]
    },
    "dark_mode": {
        "title": "Dark Mode Support",
        "description": "Toggle between light/dark UI themes",
        "constraints": ["WCAG contrast ratios", "theme persistence"]
    }
}

# Load persisted requirements from disk (seeded by setdefault() calls below)
_load_requirements()

ECOMMERCE_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
BUGTRACKR_PROJECT_ID = "18347cc8-96e8-44f2-9c26-2be7c2953ca3"

BUGTRACKR_REQUIREMENTS = [
    {
        "id": "REQ-BT-001",
        "req_id": "REQ-BT-001",
        "title": "Bug CRUD Operations",
        "description": "Users can create new bugs with title, description, priority, and status. Users can view bug details, update bug fields, and delete bugs. The bug list supports filtering and sorting.",
        "priority": "P0",
        "risk_score": 8.0,
        "impact": 3,
        "probability": 3,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-001-01",
                "statement": "User can create a new bug with title, description, and priority",
                "given": "I am on the BugTrackr dashboard",
                "when": "I click New Bug and fill in title 'Login broken', priority 'High', description 'Button unresponsive'",
                "then": "The bug is created and appears in the bug list with status 'Open'",
                "covered": False,
                "coverage_level": "NONE"
            },
            {
                "id": "AC-BT-001-02",
                "statement": "User can view bug details by clicking on a bug",
                "given": "A bug 'Login broken' exists in the system",
                "when": "I click on the bug title in the list",
                "then": "I see the bug detail page with title, description, priority, status, and timestamps",
                "covered": False,
                "coverage_level": "NONE"
            },
            {
                "id": "AC-BT-001-03",
                "statement": "User can update bug fields",
                "given": "I am viewing bug 'Login broken' detail page",
                "when": "I change the priority to 'Critical' and save",
                "then": "The bug priority is updated and the change is reflected in the list",
                "covered": False,
                "coverage_level": "NONE"
            },
            {
                "id": "AC-BT-001-04",
                "statement": "User can delete a bug",
                "given": "A bug 'Test bug' exists",
                "when": "I click delete and confirm",
                "then": "The bug is removed from the list",
                "covered": False,
                "coverage_level": "NONE"
            }
        ],
        "entities": ["Bug", "User"],
        "constraints": ["title is required", "priority must be Low/Medium/High/Critical"],
        "coverage_pct": 0.0,
        "test_count": 0
    },
    {
        "id": "REQ-BT-002",
        "req_id": "REQ-BT-002",
        "title": "Bug Status Workflow",
        "description": "Bugs follow a defined status workflow: Open -> In Progress -> Resolved -> Closed. Only valid transitions are allowed. Status changes are logged in the activity history.",
        "priority": "P0",
        "risk_score": 7.0,
        "impact": 3,
        "probability": 2,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-002-01",
                "statement": "Bug status can transition from Open to In Progress",
                "given": "A bug exists with status 'Open'",
                "when": "I change the status to 'In Progress'",
                "then": "The status updates and the transition is recorded in activity log",
                "covered": False, "coverage_level": "NONE"
            },
            {
                "id": "AC-BT-002-02",
                "statement": "Bug can be resolved and closed",
                "given": "A bug is 'In Progress'",
                "when": "I mark it as 'Resolved' then 'Closed'",
                "then": "Each transition succeeds and is logged",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["Bug", "StatusTransition", "ActivityLog"],
        "constraints": ["Only valid transitions allowed", "Cannot skip statuses"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-003",
        "req_id": "REQ-BT-003",
        "title": "Role-Based Access Control",
        "description": "The system supports Admin, Developer, and Tester roles. Admins can manage users and all bugs. Developers can create and update bugs. Testers can create bugs and add comments.",
        "priority": "P0",
        "risk_score": 9.0,
        "impact": 3,
        "probability": 3,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-003-01",
                "statement": "Admin user has full access to all features",
                "given": "I am logged in as admin",
                "when": "I navigate to user management and bug management",
                "then": "I can create users, delete bugs, and modify all settings",
                "covered": False, "coverage_level": "NONE"
            },
            {
                "id": "AC-BT-003-02",
                "statement": "Developer can create and update bugs but not delete",
                "given": "I am logged in as developer",
                "when": "I try to delete a bug",
                "then": "The delete action is denied or hidden",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["User", "Role", "Permission"],
        "constraints": ["Role hierarchy: Admin > Developer > Tester"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-004",
        "req_id": "REQ-BT-004",
        "title": "Comment System",
        "description": "Users can add comments to bugs. Comments display the author, timestamp, and content. Comments support basic text formatting.",
        "priority": "P1",
        "risk_score": 5.0,
        "impact": 2,
        "probability": 2,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-004-01",
                "statement": "User can add a comment to a bug",
                "given": "I am viewing a bug detail page",
                "when": "I type a comment and click Submit",
                "then": "The comment appears below with my name and timestamp",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["Comment", "Bug", "User"],
        "constraints": ["Comment cannot be empty"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-005",
        "req_id": "REQ-BT-005",
        "title": "Activity Tracking & Audit Logs",
        "description": "All changes to bugs are tracked in an activity log. Each entry shows who made the change, what changed, and when.",
        "priority": "P1",
        "risk_score": 6.0,
        "impact": 2,
        "probability": 3,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-005-01",
                "statement": "Status changes appear in activity log",
                "given": "A bug status is changed from Open to In Progress",
                "when": "I view the bug's activity log",
                "then": "I see an entry showing the status change with user and timestamp",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["ActivityLog", "Bug", "User"],
        "constraints": ["All mutations must be logged"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-006",
        "req_id": "REQ-BT-006",
        "title": "Project Dashboard",
        "description": "The dashboard shows bug statistics: total bugs, open bugs, resolved bugs, bugs by priority, and recent activity.",
        "priority": "P2",
        "risk_score": 4.0,
        "impact": 2,
        "probability": 2,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-006-01",
                "statement": "Dashboard shows correct bug counts",
                "given": "There are 5 open bugs and 3 resolved bugs",
                "when": "I view the dashboard",
                "then": "I see Total: 8, Open: 5, Resolved: 3",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["Dashboard", "Bug", "Statistics"],
        "constraints": ["Counts must be real-time accurate"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-007",
        "req_id": "REQ-BT-007",
        "title": "Health Check API",
        "description": "The application exposes a /api/health endpoint that returns status 200 with system health information including database connectivity.",
        "priority": "P0",
        "risk_score": 6.0,
        "impact": 3,
        "probability": 2,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-007-01",
                "statement": "Health endpoint returns 200",
                "given": "The application is running",
                "when": "I send GET /api/health",
                "then": "I receive HTTP 200 with status 'ok'",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["API", "HealthCheck"],
        "constraints": ["Must respond within 5 seconds"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-BT-008",
        "req_id": "REQ-BT-008",
        "title": "Dark Mode Support",
        "description": "The application supports dark mode theme toggle. User preference is persisted across sessions.",
        "priority": "P2",
        "risk_score": 3.0,
        "impact": 1,
        "probability": 3,
        "source_type": "manual",
        "status": "approved",
        "project_id": BUGTRACKR_PROJECT_ID,
        "acceptance_criteria": [
            {
                "id": "AC-BT-008-01",
                "statement": "User can toggle dark mode",
                "given": "I am on any page in light mode",
                "when": "I click the dark mode toggle",
                "then": "The UI switches to dark theme and the preference is saved",
                "covered": False, "coverage_level": "NONE"
            }
        ],
        "entities": ["Theme", "UserPreference"],
        "constraints": ["Preference persists via localStorage or cookie"],
        "coverage_pct": 0.0, "test_count": 0
    }
]

MOCK_REQUIREMENTS = [
    {
        "id": "REQ-017", "title": "Checkout Payment Processing", "priority": "P0",
        "risk_score": 9.4, "type": "functional",
        "coverage_pct": 87.0, "ac_count": 5, "test_count": 12,
        "constraints": ["PCI-DSS", "max 3s response", "no card data in logs"],
        "entities": ["Card", "Order", "Transaction", "User"],
        "acceptance_criteria": [
            {"id": "AC-001", "statement": "Valid payment processes within 3s", "covered": True},
            {"id": "AC-002", "statement": "Invalid card rejected gracefully", "covered": True},
            {"id": "AC-003", "statement": "Security attacks blocked and logged", "covered": True},
            {"id": "AC-004", "statement": "Timeout recovery with no double charge", "covered": False},
            {"id": "AC-005", "statement": "3DS triggered for transactions > £150", "covered": True},
        ],
    },
    {
        "id": "REQ-018", "title": "User Cart Management", "priority": "P1",
        "risk_score": 7.1, "type": "functional",
        "coverage_pct": 100.0, "ac_count": 4, "test_count": 8,
        "constraints": ["cart persists 30 days", "max 50 items"],
        "entities": ["Cart", "CartItem", "Product"],
        "acceptance_criteria": [
            {"id": "AC-006", "statement": "Add item updates cart count immediately", "covered": True},
            {"id": "AC-007", "statement": "Remove item decrements count", "covered": True},
            {"id": "AC-008", "statement": "Cart persists across sessions", "covered": True},
            {"id": "AC-009", "statement": "Max quantity enforced per item", "covered": True},
        ],
    },
    {
        "id": "REQ-019", "title": "Refund & Return Flow", "priority": "P1",
        "risk_score": 7.8, "type": "functional",
        "coverage_pct": 0.0, "ac_count": 4, "test_count": 0,
        "constraints": ["GDPR", "refund within 5 business days"],
        "entities": ["Refund", "Order", "Payment"],
        "acceptance_criteria": [
            {"id": "AC-010", "statement": "Full refund processed within 5 business days", "covered": False},
            {"id": "AC-011", "statement": "Partial refund on multi-item orders", "covered": False},
            {"id": "AC-012", "statement": "Refund confirmation email sent", "covered": False},
            {"id": "AC-013", "statement": "Return label generated automatically", "covered": False},
        ],
    },
    {
        "id": "REQ-020", "title": "Product Search & Filter", "priority": "P2",
        "risk_score": 5.8, "type": "functional",
        "coverage_pct": 71.0, "ac_count": 6, "test_count": 14,
        "constraints": ["results within 500ms", "ElasticSearch backed"],
        "entities": ["Product", "Category", "SearchQuery"],
        "acceptance_criteria": [
            {"id": "AC-014", "statement": "Search returns results within 500ms", "covered": True},
            {"id": "AC-015", "statement": "Filter by category, price, rating", "covered": True},
            {"id": "AC-016", "statement": "No results state handled gracefully", "covered": True},
            {"id": "AC-017", "statement": "Plural/singular query variants return same results", "covered": False},
            {"id": "AC-018", "statement": "Search history saved per user", "covered": False},
            {"id": "AC-019", "statement": "Autocomplete appears after 2 characters", "covered": True},
        ],
    },
]


ANALYTICS_DEMO_PROJECT_ID = "a1b2c3d4-5678-4ef0-abcd-1234567890ab"

ANALYTICS_DEMO_REQUIREMENTS = [
    # ═══════════════════════════════════════════════════════════════════════════
    # FLOW 1: ADMIN ONBOARDING  (register → org → workspace → project)
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-001", "req_id": "REQ-AN-001",
        "title": "User Registration & Multi-Provider Authentication",
        "description": "Users register and authenticate via Google OAuth, Microsoft MSAL, GitHub, Facebook, or email/password through Firebase. Social login callbacks issue JWTs. Root user login provides elevated admin access. User invitations with invite codes allow team growth.",
        "priority": "P0", "risk_score": 9.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-001-01", "statement": "Google OAuth login issues valid JWT", "given": "User clicks 'Sign in with Google'", "when": "OAuth callback POST /authentication/google/callback receives auth code", "then": "JWT issued, user record created, session established, redirect to dashboard", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-001-02", "statement": "Unauthenticated requests return 401", "given": "No JWT in Authorization header", "when": "Any protected endpoint called", "then": "HTTP 401 returned, not 500", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-001-03", "statement": "Expired JWT tokens rejected gracefully", "given": "JWT expired 1 hour ago", "when": "Protected endpoint called", "then": "401 with 'token expired'; client redirects to login", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-001-04", "statement": "User invitation creates pending invite", "given": "Admin calls POST /invite/user/{subscription_id}", "when": "Invite created", "then": "Code generated, email sent, valid for 7 days", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-001-05", "statement": "Invited user redeems code and joins org", "given": "Valid invite code", "when": "User registers with code", "then": "User added to org with assigned role", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["User", "JWT", "OAuth", "Firebase", "InviteCode", "Session"],
        "constraints": ["Google, Microsoft, GitHub, Facebook, email/password", "JWT RSA signing", "Invite codes expire 7 days"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-002", "req_id": "REQ-AN-002",
        "title": "Organization, Workspace & Project Setup",
        "description": "After login, users create an Organization with credit balance. Then Workspaces scoped to extraction or analytics service. Within a workspace, Projects are created with analytics references. Hierarchy: Organization → Workspace → Project. Role-based team management (owner, admin, viewer).",
        "priority": "P0", "risk_score": 8.5, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-002-01", "statement": "Organization created with free credits", "given": "Newly registered user", "when": "Org created with name and plan", "then": "Org has free credit bundle, creator is owner", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-002-02", "statement": "Workspace creation scoped to org and service", "given": "User in org", "when": "POST /mgmt/event/workspace with service=['extraction']", "then": "Workspace created, visible only to org members", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-002-03", "statement": "Project creation initializes analytics references", "given": "Workspace with analytics service", "when": "POST /mgmt/event/project", "then": "Project created with analytics_project_id and analytics_app_id", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-002-04", "statement": "Role management restricts access", "given": "Owner adds 'viewer' user", "when": "Viewer logs in", "then": "Can view data, cannot create/delete workspaces or projects", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["Organization", "Workspace", "Project", "CreditBalance", "UserRole"],
        "constraints": ["Hierarchy: Org → Workspace → Project", "Service types: extraction, analytics", "Free credits on org creation"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-003", "req_id": "REQ-AN-003",
        "title": "gRPC Authorization & License Verification",
        "description": "All API calls authorized via gRPC service: AuthenticateAndAuthorizeRequest, AuthorizeProjectResourceRequest, AuthorizeCollectionResourceRequest. License verification on startup and per-request. Permission-based access for monitoring, extraction, and analytics.",
        "priority": "P0", "risk_score": 9.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-003-01", "statement": "gRPC validates subscriber + subscription", "given": "Valid JWT", "when": "AuthenticateAndAuthorizeRequest called", "then": "Succeeds within 200ms", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-003-02", "statement": "Cross-project access blocked", "given": "User has Project A but not B", "when": "AuthorizeProjectResourceRequest for B", "then": "403 Forbidden", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-003-03", "statement": "Expired license blocks access", "given": "Valid JWT, expired license", "when": "Any endpoint called", "then": "403 with license expiry details", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["gRPC", "License", "Policy", "Permission", "Subscriber"],
        "constraints": ["gRPC < 200ms", "RSA license validation", "All services at startup"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # Repos: pipeline-api, Onprem-extraction, pdf-parser
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-004", "req_id": "REQ-AN-004",
        "title": "Document Type Definition & Schema Auto-Generation",
        "description": "Users define document types (Invoice, PO, COA, Bill of Lading) within a project. System auto-generates extraction schema by analyzing a sample document with OCR + LLM. Schema includes field names, types, validation rules. Users tune schema (add/remove/rename fields) before extraction. Schema cache in Redis.",
        "priority": "P0", "risk_score": 9.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-004-01", "statement": "Document type created with purpose and hints", "given": "User in extraction project", "when": "POST /mgmt/event/document-type", "then": "Doc type created, available for schema generation", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-004-02", "statement": "Schema auto-generation from sample document", "given": "Sample invoice PDF uploaded", "when": "POST /schema/event/generate-schema", "then": "LLM+OCR returns JSON schema with fields, types, validation rules", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-004-03", "statement": "Schema tuning: add/remove/rename fields", "given": "Auto-generated schema", "when": "User modifies and validates", "then": "POST /schema/event/validate-schema succeeds, schema saved", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-004-04", "statement": "Validation catches invalid configs", "given": "Integer type on text-only field", "when": "Validation runs", "then": "Error with field and reason", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["DocumentType", "Schema", "Field", "OCR", "LLM", "Validation"],
        "constraints": ["PaddleOCR + LLM (Gemini/OpenAI/Claude)", "Schemas versioned", "Redis cache"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-005", "req_id": "REQ-AN-005",
        "title": "Document Extraction Pipeline (Cloud — v1/v2/v3)",
        "description": "Three pipeline versions extract entities from 15+ document types (invoices, bank statements, COAs, shipping/customs/trade docs) using defined schemas. 95% accuracy target with confidence scores. PDF orientation correction via CNN, table extraction with bounding boxes, OCR fallback, entity approval workflow, status polling. Output: Excel/CSV/JSON or API/webhook delivery.",
        "priority": "P0", "risk_score": 9.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-005-01", "statement": "Single doc extraction with confidence > 0.8", "given": "Invoice: vendor='Acme', amount=$1,234.56, date=2026-01-15", "when": "POST /pipeline/event/execute", "then": "Entities match within 1%, confidence > 0.8", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-02", "statement": "Batch extraction (v2) concurrent processing", "given": "3 docs (PDF, DOCX, XLSX)", "when": "POST /pipeline/event/execute-v2", "then": "All 3 via Celery, individual results each", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-03", "statement": "Rotated PDF corrected before extraction", "given": "90-degree rotated PDF", "when": "CNN detects rotation", "then": "Corrected, entities extracted correctly", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-04", "statement": "Table extraction preserves structure", "given": "5×10 table in PDF", "when": "table_format='html'", "then": "50 cells with row/col mapping and bboxes", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-05", "statement": "OCR fallback for scanned PDFs", "given": "Image-only PDF", "when": "No text layer found", "then": "PaddleOCR invoked, text with bboxes", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-06", "statement": "Entity approval workflow", "given": "Low-confidence results", "when": "POST /extraction/event/approve-extracted-entities", "then": "Approved saved, rejected flagged for re-extraction", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-07", "statement": "Business rule validation on extracted data", "given": "Rules: 'amount must be positive', 'date within 1 year'", "when": "Extraction completes", "then": "Validation runs, flagged fields highlighted", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-005-08", "statement": "Data exported as Excel/CSV/JSON", "given": "Approved extraction results", "when": "User clicks download", "then": "File generated in selected format with all extracted fields", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["Pipeline", "Extraction", "Entity", "Table", "OCR", "BoundingBox", "Validation", "Export"],
        "constraints": ["Celery: hard 450s, soft 250s, acks_late", "7 doc formats", "95% accuracy target", "Output: Excel/CSV/JSON/API/webhook"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-006", "req_id": "REQ-AN-006",
        "title": "On-Premise Extraction (Ollama — Air-Gapped)",
        "description": "Self-hosted extraction using Ollama (qwen3:8b primary, qwen3:32b fallback) for air-gapped deployments. UPskill enhancement via SKILL.md files. Chunked extraction for large documents. No external API calls.",
        "priority": "P1", "risk_score": 7.5, "impact": 3, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-006-01", "statement": "Extraction completes on-premise only", "given": "qwen3:8b via Ollama", "when": "PDF submitted", "then": "No external API calls, entities in JSON", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-006-02", "statement": "Fallback to larger model on failure", "given": "qwen3:8b timeout", "when": "Retry", "then": "qwen3:32b completes extraction", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-006-03", "statement": "UPskill SKILL.md improves accuracy", "given": "Custom SKILL.md for 'Invoice'", "when": "Extraction", "then": "Skill prepended to prompt, accuracy improves", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-006-04", "statement": "Chunked extraction for 50+ pages", "given": "60-page contract", "when": "Submitted", "then": "Split, parallel extract, results merged", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["Ollama", "UPskill", "ChunkedExtraction", "LocalLLM"],
        "constraints": ["No external API", "Temp 0.1", "Qwen3 thinking disabled", "Max 16384 tokens"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-007", "req_id": "REQ-AN-007",
        "title": "Multi-Format Document Parser Service",
        "description": "Parses 7 formats: PDF (Marker + EasyOCR + PaddleOCR), Excel, DOCX, CSV, TXT, Markdown via custom wheel parsers. Dual: FastAPI sync + Celery/SQS async batch. Handles handwritten docs and scans.",
        "priority": "P1", "risk_score": 7.0, "impact": 3, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-007-01", "statement": "All 7 format parsers correct", "given": "One per format", "when": "POST /parser/extract", "then": "Auto-detected, correct parser, structured output", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-007-02", "statement": "PDF preserves structure", "given": "Multi-page mixed content", "when": "Parsed", "then": "Headers, paragraphs, tables segmented", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-007-03", "statement": "Async consumer handles queue", "given": "5 docs in RabbitMQ", "when": "Consumer runs", "then": "All parsed, results in MongoDB", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-007-04", "statement": "Scanned PDF falls back to OCR", "given": "Image-only PDF", "when": "No text found", "then": "OCR extracts text with bboxes", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["PDFExtractor", "ExcelExtractor", "DocsExtractor", "CsvExtractor", "OCR"],
        "constraints": ["Custom wheels", "PaddleOCR GPU", "Sync API + async consumer"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # FLOW 3: DATA SOURCE CONFIG & MONITORING
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-008", "req_id": "REQ-AN-008",
        "title": "Data Source Connections & Monitoring Job Management",
        "description": "Users configure data source connections (Gmail, Google Drive, OneDrive, Outlook, Links, Direct Files) with OAuth credentials. Monitoring API creates jobs, routes Celery tasks to 5 priority queues, tracks status, handles revocation. Also supports extraction triggers (GDrive, OneDrive, S3, Azure Blob, Webhook).",
        "priority": "P0", "risk_score": 8.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-008-01", "statement": "Job routes to correct queue per connection type", "given": "Gmail connection", "when": "POST .../job-create", "then": "Task to 'gmail' queue, job_id in job_store", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-008-02", "statement": "Multi-connection job creates parallel tasks", "given": "3 GDrive connections", "when": "Job created", "then": "3 tasks to 'gdrive' queue", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-008-03", "statement": "Duplicate job ID rejected", "given": "Existing job_id", "when": "Same ID submitted", "then": "Error, no duplicate", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-008-04", "statement": "Job deletion revokes Celery tasks", "given": "Active job", "when": "DELETE", "then": "Revoked, TTL expiry set", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["MonitoringJob", "CeleryQueue", "Connection", "Trigger", "JobStore"],
        "constraints": ["5 queues", "Min 601s interval", "Hard 3600s timeout", "5 retries exponential backoff"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-009", "req_id": "REQ-AN-009",
        "title": "Email Monitoring Workers (Gmail + Outlook)",
        "description": "Gmail: polls via Gmail API, subject/target email filters, downloads 14 attachment types to S3, Gemini AI extracts structured fields from email body. Outlook: Microsoft Graph Mail.Read, MSAL OAuth (delegated + app). Both deduplicate by message_id, self-reschedule via Celery countdown.",
        "priority": "P1", "risk_score": 7.0, "impact": 3, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-009-01", "statement": "Gmail filters by subject and target emails", "given": "5 emails, 2 match", "when": "gmail.process", "then": "Only 2 processed, attachments to S3", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-009-02", "statement": "Email body extraction via Gemini", "given": "Body with dates, amounts, vendors", "when": "Gemini processes with body_schema", "then": "JSON: {summary, intent, dates, amounts, names}", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-009-03", "statement": "Duplicates skipped", "given": "Previously processed message_id", "when": "Re-encountered", "then": "Skipped, no duplicate uploads", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-009-04", "statement": "Outlook delegated + app permissions", "given": "Delegated OAuth", "when": "outlook.process", "then": "MSAL token, emails read", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-009-05", "statement": "Rate limit backoff (29 retries)", "given": "HTTP 429", "when": "Retry", "then": "Exponential backoff, eventual success or failure", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["GmailAPI", "GraphAPI", "MSAL", "GeminiAI", "S3Upload", "MessageID"],
        "constraints": ["14 file extensions", "601s poll", "29 retries", "Self-rescheduling"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-010", "req_id": "REQ-AN-010",
        "title": "Cloud Storage Monitoring (GDrive + OneDrive)",
        "description": "Polling-based folder watchers. GDrive: lists children via Drive API, compares state. OneDrive: recursive crawl via Graph API. Detect new/modified/deleted files, download, upload to S3. Transparent OAuth token refresh.",
        "priority": "P1", "risk_score": 6.5, "impact": 3, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-010-01", "statement": "New files detected and uploaded", "given": "2 new PDFs in folder", "when": "Worker runs", "then": "Detected, downloaded, S3 uploaded", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-010-02", "statement": "Modified files re-downloaded", "given": "Newer modified_time", "when": "Comparison", "then": "Re-downloaded, metadata updated", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-010-03", "statement": "OneDrive recursive crawl", "given": "3 nesting levels with files", "when": "_crawl_folder_once()", "then": "All nested files discovered", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-010-04", "statement": "OAuth refresh transparent", "given": "Expired token", "when": "API call attempted", "then": "Refreshed, call succeeds, new token stored", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-010-05", "statement": "Deletions detected by set difference", "given": "File removed from Drive", "when": "State comparison", "then": "Deletion detected, file_store updated", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["DriveAPI", "GraphAPI", "FolderCrawl", "FileState", "OAuth"],
        "constraints": ["Polling, not webhooks", "State in MongoDB", "S3 key: file_analytics/.../"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-011", "req_id": "REQ-AN-011",
        "title": "Links/URL Monitoring (3-Tier Change Detection)",
        "description": "Three-tier: (1) HTTP headers (ETag+Last-Modified), (2) content hash (HTML→Markdown MD5), (3) screenshot hash (Selenium headless Chrome SHA256). Gemini for image descriptions. Cost tracking via CostPublisher.",
        "priority": "P2", "risk_score": 5.5, "impact": 2, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-011-01", "statement": "Header skip for unchanged", "given": "Same ETag+Last-Modified", "when": "Check", "then": "Scrape skipped", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-011-02", "statement": "Content hash detects text changes", "given": "Changed body, no ETag", "when": "MD5 compared", "then": "New hash, re-indexed", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-011-03", "statement": "Screenshot captures lazy-loaded content", "given": "Infinite scroll page", "when": "Selenium scrolls", "then": "Full screenshot after load", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["URL", "Selenium", "ContentHash", "ScreenshotHash", "GeminiAI"],
        "constraints": ["3-tier detection", "Selenium headless", "Cost per-file tracking"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-012", "req_id": "REQ-AN-012",
        "title": "NL→SQL Query Engine (Natural Language to SQL)",
        "description": "Users ask questions in plain English, system generates SQL against connected databases (PostgreSQL, MySQL, MongoDB, Snowflake). Uses RAG pipeline: intent analysis → table/column discovery → SQL generation → error correction loop (up to 5 retries) → result formatting. Implemented in db_rag.py with router, retriever, and SQL generator prompts.",
        "priority": "P0", "risk_score": 9.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-012-01", "statement": "Simple query generates valid SQL", "given": "Connected PostgreSQL with sales table", "when": "User asks 'Top vendors by spend last quarter'", "then": "SQL with correct GROUP BY, ORDER BY DESC, date filter; result matches manual query", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-012-02", "statement": "Multi-table join query works", "given": "Schema with orders + customers tables", "when": "User asks 'Average order value per customer segment'", "then": "JOIN generated correctly, aggregation produces accurate results", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-012-03", "statement": "Ambiguous metric names handled", "given": "Both 'revenue' and 'gross_revenue' columns", "when": "User asks 'Show me performance'", "then": "System clarifies or selects most relevant with explanation", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-012-04", "statement": "SQL error correction retries up to 5 times", "given": "First SQL attempt has syntax error", "when": "Execution fails", "then": "Error fed back to LLM, corrected SQL generated, up to 5 retries", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-012-05", "statement": "Works across PostgreSQL, MySQL, MongoDB, Snowflake", "given": "4 different database connections", "when": "Same natural language query sent to each", "then": "Correct dialect SQL generated per database type", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["NLtoSQL", "RAG", "SQLGenerator", "DatabaseConnector", "IntentAnalysis"],
        "constraints": ["Supports PostgreSQL, MySQL, MongoDB, Snowflake", "5-retry error correction", "Temperature=0 for SQL"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-013", "req_id": "REQ-AN-013",
        "title": "Dataset Creation, File Indexing & Database Connectors",
        "description": "Users create Datasets from multiple sources: monitored files (indexed by files-consumer with vector+BM25), Excel (Polars via excel-consumer), and live database connections (PostgreSQL, MySQL, MongoDB, Snowflake via db connectors). Cross-source queries combine documents and databases seamlessly.",
        "priority": "P0", "risk_score": 8.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-013-01", "statement": "PDF indexing creates vector + BM25 entries", "given": "PDF in S3", "when": "Files-consumer processes", "then": "Text chunked, embeddings generated, stored with search index", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-013-02", "statement": "Excel loaded into Polars", "given": "XLSX with 3 sheets", "when": "Excel-consumer processes", "then": "All sheets as DataFrames, metadata stored", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-013-03", "statement": "Database connector validates connection", "given": "PostgreSQL credentials", "when": "Connection test", "then": "Schema detected, tables enumerable", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-013-04", "statement": "Cross-source query works", "given": "Dataset with PDFs + PostgreSQL", "when": "User queries", "then": "Results from both documents and database combined", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["Dataset", "VectorIndex", "BM25", "DatabaseConnector", "Polars"],
        "constraints": ["MongoDB Atlas Vector Search", "BM25+vector 0.5/0.5", "4 DB connectors"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-014", "req_id": "REQ-AN-014",
        "title": "Liveboards & Auto-Visualizations (Dashboard Agent)",
        "description": "Interactive dashboards (Liveboards) with real-time data refresh and drill-down. Dashboard agent (analytics dashboard consumer) auto-selects chart types (line, bar, pie, scatter, heatmap) based on data shape. Generates MongoDB aggregation pipelines or Python code for complex visualizations. Plotly-based rendering with auto-detection: time series→line, categorical→bar, proportions→pie.",
        "priority": "P0", "risk_score": 8.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-014-01", "statement": "Auto chart type selection based on data", "given": "Time series data (date + revenue)", "when": "Chart generated", "then": "Line chart auto-selected (not bar/pie)", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-014-02", "statement": "Dashboard agent generates MongoDB aggregation", "given": "Request: 'Show monthly sales trend'", "when": "Dashboard agent processes", "then": "MongoDB aggregation pipeline generated, executed, chart data returned", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-014-03", "statement": "Liveboard drill-down works interactively", "given": "Bar chart showing revenue by region", "when": "User clicks a region bar", "then": "Drill-down shows sub-region breakdown", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-014-04", "statement": "Real-time dashboard refresh", "given": "Liveboard connected to live data", "when": "Underlying data changes", "then": "Dashboard refreshes within configured interval", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-014-05", "statement": "6 chart types render correctly", "given": "Appropriate data for each: line, bar, hbar, pie, scatter, heatmap", "when": "Each chart type generated", "then": "Valid Plotly JSON, correct axes, data traces, dark theme", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["Liveboard", "DashboardAgent", "Plotly", "MongoDBAggregation", "ChartAutoDetection"],
        "constraints": ["6 chart types", "Auto-detection logic", "Plotly dark theme", "Data reduction for large sets"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-015", "req_id": "REQ-AN-015",
        "title": "AI File Chat Agent (9 MCP Tools)",
        "description": "Agentic file chat via 9 MCP tools (port 9009 SSE): hybrid context retrieval (BM25+vector), 3-level single-doc summaries, 3-level multi-doc syntheses, query refinement with conversation history, query decomposition. Multi-LLM: Gemini, OpenAI, Claude, Groq, Ollama, DeepSeek.",
        "priority": "P0", "risk_score": 8.5, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-015-01", "statement": "Hybrid retrieval returns grounded results", "given": "Indexed sales dataset", "when": "context_retriever: 'revenue by vendor'", "then": "Top-5 with source attribution", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-015-02", "statement": "Summary levels are distinct", "given": "20-page doc", "when": "Short vs detailed", "then": "Short: 2-3 sentences. Detailed: all sections", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-015-03", "statement": "Multi-doc synthesis across files", "given": "3 quarterly reports", "when": "multi_doc_medium_summary", "then": "Trends across all 3 quarters", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-015-04", "statement": "Query refinement uses history", "given": "5 prior messages about vendors", "when": "'show me the top 3'", "then": "Refined: 'top 3 vendors by spend'", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-015-05", "statement": "No hallucination — grounded insights", "given": "Query about indexed metrics", "when": "Insight generated", "then": "Every claim traceable to source with page/section", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["MCP", "ContextRetriever", "Summary", "QueryRefine", "QueryDecompose"],
        "constraints": ["9 tools, port 9009 SSE", "BM25+vector 0.5/0.5", "6 LLM providers"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-016", "req_id": "REQ-AN-016",
        "title": "Excel Analytics Agent (29 MCP Tools)",
        "description": "Excel analysis via 29 MCP tools (port 9011): data access (6), profiling (8: stats, correlation, outliers IQR/Z-score, patterns), processing (4: Polars, merge, sandboxed code), visualization (2: Plotly charts), memory (8: Redis TTL 1h/30d). Supports Excel, CSV, Parquet, JSON.",
        "priority": "P0", "risk_score": 8.5, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-016-01", "statement": "Statistical profiling accurate", "given": "'revenue' column, 100 rows", "when": "describe_data_tool", "then": "Mean, median, std within 0.01%", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-016-02", "statement": "Outlier detection finds anomalies", "given": "3 outliers (Z>3)", "when": "Z-score detection", "then": "All 3 found with scores", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-016-03", "statement": "Correlation detects relationships", "given": "revenue↔ad_spend r=0.95", "when": "correlation_analysis", "then": "Pearson = 0.95±0.02", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-016-04", "statement": "Chart from instruction valid", "given": "Date+revenue", "when": "'line chart revenue over time'", "then": "Valid Plotly JSON", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-016-05", "statement": "Code sandbox blocks dangerous ops", "given": "os.system attack", "when": "execute_code_tool", "then": "Blocked", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-016-06", "statement": "Multi-file merge works", "given": "2 Excel + 1 CSV matching columns", "when": "merge_files", "then": "Merged, total rows = sum", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["ExcelMCP", "Polars", "Plotly", "CodeSandbox", "Memory", "Outlier"],
        "constraints": ["29 tools, port 9011", "Redis TTL 1h/30d", "Sandboxed code"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-017", "req_id": "REQ-AN-017",
        "title": "Embeddable Analytics & Chatbot Deployment",
        "description": "Analytics can be embedded in customer applications via S3-hosted script tag injection. Chatbot launch generates embeddable snippet with auth token support (Google, Azure). Configurable styling and persona.",
        "priority": "P2", "risk_score": 5.0, "impact": 2, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-017-01", "statement": "Embed script tag generated correctly", "given": "AI app with chatbot enabled", "when": "User clicks 'Get Embed Code'", "then": "Script tag with S3 URL and config generated, copy-to-clipboard works", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-017-02", "statement": "Embedded chatbot authenticates", "given": "Embed script on external website", "when": "User interacts with chatbot", "then": "Auth token validated, responses generated from indexed dataset", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["EmbedScript", "Chatbot", "S3Config", "AuthToken"],
        "constraints": ["S3-hosted script", "Auth token: Google or Azure"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # FLOW 5: BILLING (credit balance + consumption)
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-018", "req_id": "REQ-AN-018",
        "title": "Credit Balance & Pay-Per-Use Consumption",
        "description": "Universal credit system: 1 credit = 1 page or 1 AI interaction. Credits never expire, shared across teams. Balance tracks 3 types: credits, pages, queries. Consumed per page (extraction) or per query (analytics) via RabbitMQ cost_calc queue. Free credits on org creation.",
        "priority": "P0", "risk_score": 8.0, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-018-01", "statement": "Balance returns all credit types", "given": "Active org", "when": "GET /get-credit-balance", "then": "{credit_balance, page_balance, query_balance}", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-018-02", "statement": "Page credit deducted on extraction", "given": "5-page doc extracted", "when": "cost_calc message processed", "then": "Page balance -5, cost log created", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-018-03", "statement": "Query credit deducted on analytics", "given": "AI chat query", "when": "LLM tokens consumed", "then": "Query credit deducted, usage logged", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-018-04", "statement": "Zero balance blocks operations", "given": "0 page credits", "when": "Extraction submitted", "then": "Rejected: 'insufficient credits'", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["CreditBalance", "PageCredit", "QueryCredit", "CostCalc"],
        "constraints": ["Credits never expire", "Shared across teams", "RabbitMQ cost_calc queue"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-019", "req_id": "REQ-AN-019",
        "title": "Analytics Web Frontend (React 18)",
        "description": "React 18 + Redux Toolkit + MUI v6. Key flows: extraction home (doc types, schema config, upload, status), analytics home (data sources, datasets, AI apps, chat, Liveboards), navigation org→workspace→project, Socket.IO real-time, PDF/DOCX preview, extraction triggers config.",
        "priority": "P1", "risk_score": 6.0, "impact": 2, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-019-01", "statement": "Upload shows real-time progress", "given": "Admin on extraction page", "when": "PDF uploaded", "then": "Socket.IO: parsing→extracting→complete", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-019-02", "statement": "Analytics chat streams responses", "given": "User in AI app", "when": "Question submitted", "then": "Token-by-token streaming, citations after", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-019-03", "statement": "Navigation hierarchy routes correctly", "given": "User with extract+analytics", "when": "Navigates full path", "then": "Orgs→workspaces→projects→doc types/datasets all load", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-019-04", "statement": "Extraction trigger configuration works", "given": "GDrive trigger configured", "when": "Trigger saved", "then": "Trigger stored, monitoring job created on activation", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["React", "Redux", "MUI", "SocketIO", "ExtractionUI", "AnalyticsUI"],
        "constraints": ["React 18 strict", "MUI v6.1.6", "4 env configs"],
        "coverage_pct": 0.0, "test_count": 0
    },
    # ═══════════════════════════════════════════════════════════════════════════
    # FLOW 7: E2E PIPELINE + NFRs
    # ═══════════════════════════════════════════════════════════════════════════
    {
        "id": "REQ-AN-020", "req_id": "REQ-AN-020",
        "title": "End-to-End: Source → Ingest → Parse → Extract → Analyze",
        "description": "Full pipeline: monitoring workers detect→S3→parser→extraction→analytics index→user queries via AI chat/NL→SQL/Liveboards. RabbitMQ+Redis+Celery orchestrate. Cost accumulated. Firebase real-time progress.",
        "priority": "P0", "risk_score": 8.5, "impact": 3, "probability": 3,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-020-01", "statement": "Gmail→extract→chat E2E within 120s", "given": "Email with invoice PDF", "when": "Full pipeline", "then": "Queryable via chat within 120s", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-020-02", "statement": "GDrive→Excel agent E2E", "given": "New Excel in Drive", "when": "Worker→index→chat", "then": "User queries Excel data", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-020-03", "statement": "No data loss on RabbitMQ restart", "given": "Processing during restart", "when": "Reconnect", "then": "Unacked redelivered, no duplicates", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-020-04", "statement": "Cost tracking across pipeline", "given": "Doc through all stages", "when": "CostPublisher events", "then": "Total = all stages summed", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["E2EPipeline", "RabbitMQ", "Redis", "Celery", "CostTracking"],
        "constraints": ["E2E < 120s", "acks_late", "Idempotent consumers"],
        "coverage_pct": 0.0, "test_count": 0
    },
    {
        "id": "REQ-AN-021", "req_id": "REQ-AN-021",
        "title": "Performance, Security & Observability (NFR)",
        "description": "P95 latency targets, Celery timeout (250s soft/450s hard), OpenTelemetry+Jaeger tracing, structlog, SonarQube, AES-256+TLS 1.3 encryption, RBAC with SSO/SAML, audit trails, SOC2 Ready.",
        "priority": "P1", "risk_score": 7.0, "impact": 3, "probability": 2,
        "source_type": "code_analysis", "status": "approved", "project_id": ANALYTICS_DEMO_PROJECT_ID,
        "acceptance_criteria": [
            {"id": "AC-AN-021-01", "statement": "P95 latency within targets", "given": "100 concurrent users", "when": "Core endpoints", "then": "P95 < 3s collection, < 5s extraction", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-021-02", "statement": "OpenTelemetry full lifecycle trace", "given": "Extraction request", "when": "Full flow", "then": "Jaeger trace with per-stage spans", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-021-03", "statement": "Celery soft timeout graceful", "given": "Task > 250s", "when": "SoftTimeLimitExceeded", "then": "Partial results saved, clean exit", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-021-04", "statement": "No critical SonarQube issues", "given": "All repos scanned", "when": "Analysis", "then": "Zero critical/blocker", "covered": False, "coverage_level": "NONE"},
            {"id": "AC-AN-021-05", "statement": "Data encrypted at rest and in transit", "given": "All services", "when": "Data stored/transmitted", "then": "AES-256 at rest, TLS 1.3 in transit", "covered": False, "coverage_level": "NONE"},
        ],
        "entities": ["OpenTelemetry", "Jaeger", "SonarQube", "AES256", "TLS", "SOC2"],
        "constraints": ["P95 targets", "AES-256 + TLS 1.3", "RBAC+SSO/SAML", "SOC2 Ready"],
        "coverage_pct": 0.0, "test_count": 0
    },
]

# Seed demo projects into PROJECT_REQUIREMENTS by project_id so the mock
# fallback can use a uniform lookup without per-project branches.
PROJECT_REQUIREMENTS.setdefault(BUGTRACKR_PROJECT_ID, list(BUGTRACKR_REQUIREMENTS))
PROJECT_REQUIREMENTS.setdefault(ECOMMERCE_PROJECT_ID, list(MOCK_REQUIREMENTS))
PROJECT_REQUIREMENTS.setdefault(ANALYTICS_DEMO_PROJECT_ID, list(ANALYTICS_DEMO_REQUIREMENTS))


@router.get("", dependencies=[Depends(_require_api_key)])
async def list_requirements(
    priority: str | None = None,
    covered: bool | None = None,
    project_id: str | None = None,
):
    """List all requirements with optional filters."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementRepo, _to_dict
            repo = RequirementRepo(db)
            rows, total = await repo.list(project_id=project_id, priority=priority)
            reqs = []
            for r in rows:
                d = _to_dict(r)
                d["acceptance_criteria"] = [
                    {"id": ac.ac_id, "statement": ac.title, "covered": ac.is_covered}
                    for ac in (r.acceptance_criteria or [])
                ]
                d["ac_count"] = len(d["acceptance_criteria"])
                reqs.append(d)
            if covered is not None:
                reqs = [r for r in reqs if (r.get("risk_score", 0) and r.get("risk_score", 0) > 0) == covered]

            # Merge in-memory requirements not yet in DB.
            # F4-3: Honour the priority filter on the in-memory merge too —
            # otherwise `?priority=P0` leaks P1/P2 entries from PROJECT_REQUIREMENTS.
            db_req_ids = {r.get("req_id") or r.get("id") for r in reqs}
            lookup_pid = project_id or ECOMMERCE_PROJECT_ID
            in_memory = PROJECT_REQUIREMENTS.get(lookup_pid, [])
            for r in in_memory:
                rid = r.get("req_id") or r.get("id")
                if rid and rid not in db_req_ids:
                    if priority and r.get("priority") != priority:
                        continue
                    reqs.append(r)

            # Compute AC coverage from available tests (per-AC matching)
            try:
                from .tests import GENERATED_TESTS
                # F20-5: Count tests from BOTH the DB and in-memory
                # GENERATED_TESTS. The previous version only counted
                # in-memory tests, which silently missed tests that
                # were persisted to DB but not in memory (e.g. tests
                # with NULL requirement_id column whose textual req_id
                # lives in metadata only — see F20-4 for the
                # hydration story). Without this, /api/tests (which
                # merges DB+memory) reports more tests than
                # /api/requirements.test_count sums to, breaking the
                # Architecture vs Test Explorer parity contract.
                from sqlalchemy import text as _text
                # F20-5: Pull the SET of distinct test_ids per requirement
                # from the DB. We then UNION this with the in-memory
                # GENERATED_TESTS set per requirement and report the
                # cardinality. This matches exactly what /api/tests does
                # in its DB+memory merge, so the two endpoints agree.
                # Using SETs (not counts) avoids double-counting tests
                # that exist in both DB and in-memory.
                db_test_ids_by_req: dict[str, set[str]] = {}
                try:
                    rows_db = await db.execute(_text("""
                        SELECT COALESCE(metadata->>'requirement_id', '') AS rid,
                               test_id
                        FROM test_cases
                        WHERE project_id = CAST(:pid AS uuid)
                    """), {"pid": project_id or ""})
                    for row in rows_db:
                        rid_text = (row[0] or "").strip()
                        tid = row[1]
                        if rid_text and tid:
                            db_test_ids_by_req.setdefault(rid_text, set()).add(tid)
                except Exception as _exc:
                    log.warning("test_count DB query failed (%s) — falling back "
                                "to in-memory count only; UI may underreport",
                                type(_exc).__name__)

                for r in reqs:
                    rid = r.get("req_id") or r.get("id")
                    # F20-2: Defensive ID-based dedup. GENERATED_TESTS is
                    # supposed to be append-only with unique test_ids,
                    # but if a future bug ever re-inserts the same id,
                    # this prevents `test_count` from silently double-
                    # counting. Pairs with F20-1's removal of title-
                    # based dedup in /api/tests so both endpoints agree
                    # on a true distinct-test count.
                    seen_ids: set[str] = set()
                    req_tests = []
                    for t in GENERATED_TESTS:
                        if t.get("requirement_id") != rid:
                            continue
                        tid = t.get("id") or t.get("test_id")
                        if tid and tid in seen_ids:
                            continue
                        if tid:
                            seen_ids.add(tid)
                        req_tests.append(t)
                    # F20-5: Union of in-memory ids + DB ids = total
                    # distinct test_ids for this req. Equals what
                    # /api/tests's merge logic returns. Without this
                    # union, DB tests not in memory are dropped from
                    # test_count but counted by /api/tests, causing the
                    # Architecture vs Test Explorer mismatch.
                    union_ids = seen_ids | db_test_ids_by_req.get(rid, set())
                    r["test_count"] = len(union_ids)

                    # Get distinct AC IDs that have tests mapped
                    covered_ac_ids = {t.get("ac_id") for t in req_tests if t.get("ac_id")}

                    # Mark individual ACs as covered only if they have a matching test
                    ac_list = r.get("acceptance_criteria", [])
                    covered_count = 0
                    if isinstance(ac_list, list):
                        for ac in ac_list:
                            if isinstance(ac, dict):
                                ac_id = ac.get("id", "")
                                if ac_id in covered_ac_ids:
                                    ac["covered"] = True
                                    ac["coverage_level"] = "FULL"
                                    covered_count += 1
                                else:
                                    ac["covered"] = False
                                    ac["coverage_level"] = "NONE"

                    total_acs = max(len(ac_list), 1)
                    r["coverage_pct"] = round(covered_count / total_acs * 100)
            except Exception:
                pass

            # Sanitize to prevent RecursionError from ORM circular references
            safe_reqs = json.loads(json.dumps(reqs, default=str))
            return {"requirements": safe_reqs, "total": len(safe_reqs)}

    # Mock fallback — uniform lookup, no per-project branches.
    # project_id=None defaults to the E-Commerce demo project.
    base_reqs = PROJECT_REQUIREMENTS.get(project_id or ECOMMERCE_PROJECT_ID, [])

    # Compute dynamic coverage from GENERATED_TESTS (applies to all projects uniformly).
    # Makes shallow copies so module-level constants are never mutated.
    try:
        from .tests import GENERATED_TESTS
        reqs = []
        for r in base_reqs:
            rc = dict(r)
            rid = rc.get("req_id") or rc.get("id")
            # F20-2: same defensive ID-based dedup as the DB path above.
            seen_ids: set = set()
            req_tests = []
            for t in GENERATED_TESTS:
                if t.get("requirement_id") != rid:
                    continue
                tid = t.get("id") or t.get("test_id")
                if tid and tid in seen_ids:
                    continue
                if tid:
                    seen_ids.add(tid)
                req_tests.append(t)
            rc["test_count"] = len(req_tests)

            # Compute per-AC coverage
            covered_ac_ids = {t.get("ac_id") for t in req_tests if t.get("ac_id")}
            ac_list = rc.get("acceptance_criteria", [])
            covered_count = 0
            if isinstance(ac_list, list):
                for ac in ac_list:
                    if isinstance(ac, dict):
                        ac_id = ac.get("id", "")
                        if ac_id in covered_ac_ids:
                            ac["covered"] = True
                            ac["coverage_level"] = "FULL"
                            covered_count += 1
                        else:
                            ac["covered"] = False
                            ac["coverage_level"] = "NONE"
            total_acs = max(len(ac_list), 1)
            rc["coverage_pct"] = round(covered_count / total_acs * 100)
            reqs.append(rc)
    except Exception:
        reqs = list(base_reqs)

    if priority:
        reqs = [r for r in reqs if r.get("priority", "") == priority.upper()]
    if covered is not None:
        reqs = [r for r in reqs if (r.get("coverage_pct", 0) > 0) == covered]
    safe_reqs = json.loads(json.dumps(reqs, default=str))
    return {"requirements": safe_reqs, "total": len(safe_reqs)}


@router.get("/{req_id}", dependencies=[Depends(_require_api_key)])
async def get_requirement(req_id: str, project_id: str | None = None):
    """Get a single requirement with full AC and test mappings.

    F12-10: when `project_id` is provided, restrict the in-memory fallback
    to that project's requirement set. Without it, the previous code
    searched across ALL demo projects + per-project stores and returned
    the first ID match — which leaked across projects whenever ids
    overlapped (e.g., REQ-001 in demo vs REQ-001 in BugTrackr).
    """
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementRepo, _to_dict
            repo = RequirementRepo(db)
            r = await repo.get(req_id.upper())
            if r:
                # F12-10: enforce project scope on the DB hit too — the
                # repo is currently keyed by req_id alone; a future schema
                # may dedupe by (project_id, req_id), so we double-check.
                if project_id and getattr(r, "project_id", None) and str(r.project_id) != project_id:
                    raise HTTPException(status_code=404, detail=f"Requirement {req_id} not in project {project_id}")
                d = _to_dict(r)
                d["acceptance_criteria"] = [
                    {"id": ac.ac_id, "statement": ac.title, "covered": ac.is_covered}
                    for ac in (r.acceptance_criteria or [])
                ]
                return d

    # F12-10: scope the in-memory fallback to the named project. When
    # project_id is absent, preserve back-compat (search across all stores)
    # but emit a warning so we can find lingering callers that should pass it.
    if project_id:
        candidate_lists: list[list] = []
        proj_reqs = PROJECT_REQUIREMENTS.get(project_id)
        if isinstance(proj_reqs, list):
            candidate_lists.append(proj_reqs)
    else:
        import logging as _logging
        _logging.getLogger("arta.requirements").warning(
            "GET /requirements/%s called without project_id — falling back to "
            "cross-project search; pass ?project_id= to enforce scoping",
            req_id,
        )
        candidate_lists = [list(MOCK_REQUIREMENTS), list(BUGTRACKR_REQUIREMENTS)]
        for proj_reqs in PROJECT_REQUIREMENTS.values():
            if isinstance(proj_reqs, list):
                candidate_lists.append(proj_reqs)
    for lst in candidate_lists:
        req = next((r for r in lst if r.get("id") == req_id.upper() or r.get("req_id") == req_id.upper()), None)
        if req:
            return req
    raise HTTPException(status_code=404, detail=f"Requirement {req_id} not found")


class BulkRequirementsCreate(BaseModel):
    project_id: str
    requirements: list[dict]


@router.post("/bulk", dependencies=[Depends(_require_api_key)])
async def bulk_create_requirements(body: BulkRequirementsCreate):
    """Store multiple requirements for a project (from generate-tests output)."""
    if body.project_id not in PROJECT_REQUIREMENTS:
        PROJECT_REQUIREMENTS[body.project_id] = []
    # Assign sequential IDs if not present
    existing_count = len(PROJECT_REQUIREMENTS[body.project_id])
    for i, req in enumerate(body.requirements):
        if "id" not in req:
            req["id"] = f"REQ-{existing_count + i + 1:03d}"
        if "coverage_pct" not in req:
            req["coverage_pct"] = 0.0
        if "test_count" not in req:
            req["test_count"] = 0
        if "ac_count" not in req:
            req["ac_count"] = len(req.get("acceptance_criteria", []))
        PROJECT_REQUIREMENTS[body.project_id].append(req)

    # Persist to DB
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import RequirementRepo
                import uuid as _uuid
                repo = RequirementRepo(db)
                for req in body.requirements:
                    await repo.create({
                        "req_id": req.get("id", req.get("req_id", "")),
                        "title": req.get("title", ""),
                        "description": req.get("description", ""),
                        "priority": req.get("priority", "P2"),
                        "acceptance_criteria": req.get("acceptance_criteria", []),
                        "project_id": _uuid.UUID(body.project_id),
                    })
                await db.commit()
    except Exception as exc:
        import logging
        logging.getLogger("arta.requirements").warning("Failed to persist bulk requirements to DB: %s", exc)

    _save_requirements()
    return {
        "message": f"Stored {len(body.requirements)} requirements for project {body.project_id}",
        "total": len(PROJECT_REQUIREMENTS[body.project_id]),
    }


@router.post("/discover", dependencies=[Depends(_require_api_key)])
async def discover_requirements(body: dict, request: Request):
    """
    Agentic requirement discovery -- uses LLM to analyze a project's
    GitHub repo (README, issues, code structure) and generate
    structured requirements with acceptance criteria.
    """
    project_id = body.get("project_id")
    sources = body.get("sources", ["github_readme", "github_issues"])

    if not project_id:
        raise HTTPException(400, "project_id is required")

    # Load project config to get GitHub repo info
    from .projects import _PROJECTS, _load_projects

    project = _PROJECTS.get(project_id)
    if not project:
        # Reload from disk in case it was recently created
        _load_projects()
        project = _PROJECTS.get(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")

    github_repo = (
        project.get("integrations", {}).get("github_repo")
        or project.get("integrations", {}).get("github", {}).get("repo", "")
    )

    if not github_repo:
        raise HTTPException(400, "No GitHub repository configured for this project")

    # Parse owner/repo
    parts = github_repo.replace("https://github.com/", "").strip("/").split("/")
    if len(parts) < 2:
        raise HTTPException(400, f"Invalid GitHub repo format: {github_repo}")
    owner, repo = parts[0], parts[1]

    # Fetch context from GitHub
    context_parts = []

    github_token = (
        project.get("integrations", {}).get("github_token", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )
    # Fine-grained PATs (github_pat_*) need Bearer, classic tokens use token
    if github_token:
        auth_prefix = "Bearer" if github_token.startswith("github_pat_") else "token"
        headers = {"Authorization": f"{auth_prefix} {github_token}"}
    else:
        headers = {}

    import httpx

    if "github_readme" in sources:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={**headers, "Accept": "application/vnd.github.raw+json"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    context_parts.append(f"# README.md\n\n{resp.text[:8000]}")
        except Exception as e:
            log.warning("Failed to fetch README: %s", e)

    if "github_issues" in sources:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/issues",
                    headers=headers,
                    params={"state": "open", "per_page": 30},
                    timeout=15,
                )
                if resp.status_code == 200:
                    issues = [i for i in resp.json() if "pull_request" not in i]
                    issues_text = "\n".join([
                        f"- Issue #{i['number']}: {i['title']}\n  Labels: {', '.join(l['name'] for l in i.get('labels', []))}\n  {(i.get('body') or '')[:500]}"
                        for i in issues[:20]
                    ])
                    context_parts.append(f"# GitHub Issues\n\n{issues_text}")
        except Exception as e:
            log.warning("Failed to fetch issues: %s", e)

    if "github_code" in sources:
        try:
            async with httpx.AsyncClient() as client:
                # Fetch route structure
                for api_path in ["src/app/api", "app/api", "pages/api", "src/pages/api", "src/routes", "app/routes"]:
                    resp = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{api_path}",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        items = resp.json()
                        if isinstance(items, list):
                            routes = [f"  - {i['path']}" for i in items]
                            context_parts.append(f"# API Routes\n\n{chr(10).join(routes)}")
                            break
        except Exception as e:
            log.warning("Failed to fetch code structure: %s", e)

    if not context_parts:
        # Could not fetch from GitHub — return existing requirements
        existing_reqs = PROJECT_REQUIREMENTS.get(project_id, [])
        return {
            "status": "completed",
            "project_id": project_id,
            "sources_analyzed": sources,
            "requirements_discovered": len(existing_reqs),
            "requirements": existing_reqs,
            "message": (
                f"Could not fetch context from GitHub (check repo URL and permissions). "
                f"Returning {len(existing_reqs)} pre-configured requirements."
            ),
        }

    combined_context = "\n\n---\n\n".join(context_parts)

    # Send to LLM for agentic analysis
    llm_client = getattr(request.app.state, "anthropic", None)
    if not llm_client:
        # No LLM available — return existing pre-configured requirements as fallback
        existing_reqs = PROJECT_REQUIREMENTS.get(project_id, [])
        return {
            "status": "completed",
            "project_id": project_id,
            "sources_analyzed": sources,
            "requirements_discovered": len(existing_reqs),
            "requirements": existing_reqs,
            "message": (
                f"No LLM configured — returning {len(existing_reqs)} pre-configured requirements. "
                "Configure an LLM provider in Settings → LLM Configuration for AI-powered discovery from GitHub."
            ),
        }

    prompt = f"""You are a senior QA architect analyzing a software project to identify testable requirements.

PROJECT: {project.get('name', 'Unknown')}
DESCRIPTION: {project.get('description', '')}
TYPE: {project.get('project_type', 'web_app')}

CONTEXT FROM REPOSITORY:
{combined_context}

TASK: Identify ALL testable requirements for this project. For each requirement, provide:

Return a JSON array of requirements. Each requirement must have:
- "id": sequential like "REQ-001", "REQ-002", etc.
- "title": concise requirement title
- "description": detailed description
- "priority": "P0" (critical), "P1" (high), "P2" (medium), or "P3" (low)
- "risk_score": 1-9 (Impact 1-3 x Probability 1-3)
- "acceptance_criteria": array of objects, each with:
  - "id": like "AC-001-01"
  - "statement": what should happen
  - "given": precondition
  - "when": action
  - "then": expected result
- "entities": array of domain entities involved
- "test_types": array of test types needed ("UI", "API", "performance", "security")

Consider:
- CRUD operations for each entity
- Role-based access control (if applicable)
- Status workflows and state transitions
- Input validation and error handling
- Edge cases (empty inputs, max lengths, duplicates, concurrent access)
- API health and error responses
- UI interactions and user flows

Return ONLY the JSON array, no other text."""

    try:
        import asyncio as _asyncio

        # Use a faster model for discovery if using Ollama (32B is too slow)
        provider = getattr(request.app.state, "llm_provider", "")
        if provider == "ollama":
            model_name = "qwen3:8b"  # Fast model for discovery
        else:
            model_name = os.environ.get("ARTA_LLM_MODEL", "claude-sonnet-4-6")

        extra_headers = getattr(request.app.state, "llm_extra_headers", {})
        create_kwargs = {
            "model": model_name,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if extra_headers:
            create_kwargs["extra_headers"] = extra_headers

        # Timeout to prevent proxy drop (Next.js proxy times out at ~30s)
        try:
            response = await _asyncio.wait_for(
                llm_client.messages.create(**create_kwargs),
                timeout=90.0,
            )
        except _asyncio.TimeoutError:
            log.warning("LLM call timed out — returning pre-configured requirements")
            existing_reqs = PROJECT_REQUIREMENTS.get(project_id, [])
            return {
                "status": "partial",
                "project_id": project_id,
                "requirements_discovered": len(existing_reqs),
                "requirements": existing_reqs,
                "message": (
                    f"LLM is still processing (model may be slow). "
                    f"Returning {len(existing_reqs)} pre-configured requirements. "
                    "Try again or use a faster model in Settings → LLM Configuration."
                ),
            }

        # response assigned from wait_for above

        # Parse LLM response
        text = response.content[0].text.strip()
        # Extract JSON from response (handle markdown code blocks)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        requirements = json.loads(text)

        # Store discovered requirements
        for req in requirements:
            req["project_id"] = project_id
            req["source_type"] = "ai_generated"
            req["coverage_pct"] = 0.0
            req["test_count"] = 0
            req["req_id"] = req.get("id", "")
            req["ac_count"] = len(req.get("acceptance_criteria", []))
        # Store discovered requirements in PROJECT_REQUIREMENTS (unified for all projects)
        if project_id not in PROJECT_REQUIREMENTS:
            PROJECT_REQUIREMENTS[project_id] = []
        for req in requirements:
            existing = next((r for r in PROJECT_REQUIREMENTS[project_id] if r.get("id") == req["id"]), None)
            if not existing:
                PROJECT_REQUIREMENTS[project_id].append(req)
        _save_requirements()

        return {
            "status": "completed",
            "project_id": project_id,
            "sources_analyzed": sources,
            "requirements_discovered": len(requirements),
            "requirements": requirements,
            "message": f"Discovered {len(requirements)} requirements from {', '.join(sources)}",
        }

    except json.JSONDecodeError as e:
        log.error("Failed to parse LLM response as JSON: %s", e)
        return {
            "status": "partial",
            "project_id": project_id,
            "requirements_discovered": 0,
            "raw_response": text[:2000] if "text" in dir() else "",
            "error": f"LLM response was not valid JSON: {str(e)}",
        }
    except Exception as e:
        log.error("Requirement discovery failed: %s", e)
        # Return existing requirements as fallback instead of crashing
        existing_reqs = PROJECT_REQUIREMENTS.get(project_id, [])
        return {
            "status": "partial",
            "project_id": project_id,
            "requirements_discovered": len(existing_reqs),
            "requirements": existing_reqs,
            "error": str(e),
            "message": (
                f"LLM analysis failed ({str(e)[:100]}). "
                f"Returning {len(existing_reqs)} pre-configured requirements."
            ),
        }


@router.post("", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def create_requirement(body: RequirementCreate, request: Request):
    """Ingest a new requirement and trigger analysis."""
    client = request.app.state.anthropic
    from ...agents.requirement_intel import RequirementIntelAgent
    agent = RequirementIntelAgent(client)
    requirements = await agent.parse_document(
        f"{body.title}\n\n{body.description}",
        body.source_type,
    )
    return {
        "message": f"Requirement ingested and analyzed",
        "requirements_created": len(requirements),
        "requirements": [agent._to_dict(r) for r in requirements],
    }


@router.post("/parse", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def parse_requirement(body: ParseRequest, request: Request):
    """
    Parse raw text into structured requirements using the LLM.
    Accepts: user stories, PRDs, API specs, acceptance criteria.
    """
    client = request.app.state.anthropic
    from ...agents.requirement_intel import RequirementIntelAgent
    agent = RequirementIntelAgent(client)
    requirements = await agent.parse_document(body.text, body.source_type)
    return {
        "requirements": [agent._to_dict(r) for r in requirements],
        "count": len(requirements),
        "source_type": body.source_type,
    }


# ── Feature 6: Multi-format file upload ───────────────────────────────────

@router.post("/upload", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def upload_requirements(
    request: Request,
    # F5-8: The previous string-literal `"UploadFile | None"` annotation triggered
    # PydanticUserError("ForwardRef not fully defined") whenever FastAPI generated
    # the OpenAPI schema, breaking `/openapi.json` (and therefore `/docs`).
    # Importing UploadFile + File at module top + using the real type fixes it.
    file: UploadFile | None = File(None),
    source_type: str = "auto",
    project_id: str | None = None,
    preview: bool = Query(False, description="When True, parse and return requirements without committing to DB"),
):
    """
    Parse requirements from an uploaded file.
    Supported: .docx, .xlsx, .pdf, .md, .txt, .json, .yaml
    """
    if file is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No file uploaded")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "txt"
    content = await file.read()

    # Detect source type from extension
    if source_type == "auto":
        ext_map = {"docx": "word", "xlsx": "excel", "pdf": "pdf", "md": "markdown",
                   "txt": "text", "json": "json", "yaml": "yaml", "yml": "yaml"}
        source_type = ext_map.get(ext, "text")

    # Parse content based on format
    text = ""
    warnings: list[str] = []

    if ext in ("md", "txt"):
        text = content.decode("utf-8", errors="replace")
    elif ext == "json":
        import json as _json
        try:
            data = _json.loads(content)
            if isinstance(data, list):
                text = "\n".join(str(item) for item in data)
            else:
                text = _json.dumps(data, indent=2)
        except Exception as e:
            warnings.append(f"JSON parse warning: {e}")
            text = content.decode("utf-8", errors="replace")
    elif ext in ("yaml", "yml"):
        text = content.decode("utf-8", errors="replace")
    elif ext == "docx":
        try:
            import docx  # python-docx
            import io
            doc = docx.Document(io.BytesIO(content))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            # Library missing — soft warning, operator-fixable. Fall back so
            # legacy uploads in CI without the dep don't 500.
            warnings.append("python-docx not installed — treating as text")
            text = content.decode("utf-8", errors="replace")
        except Exception as e:
            # Phase 1.3: real parse failure → 422. The previous fallback
            # decoded raw .docx XML/zipped bytes as utf-8 and fed garbage
            # to the LLM, producing fabricated requirements.
            from fastapi import HTTPException
            raise HTTPException(
                status_code=422,
                detail=f"Could not parse {file.filename} as DOCX: {e}",
            )
    elif ext == "xlsx":
        try:
            import openpyxl
            import io
            wb = openpyxl.load_workbook(io.BytesIO(content))
            ws = wb.active
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append("\t".join(str(c or "") for c in row))
            text = "\n".join(rows)
        except ImportError:
            warnings.append("openpyxl not installed — cannot parse Excel")
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=422,
                detail=f"Could not parse {file.filename} as XLSX: {e}",
            )
    elif ext == "pdf":
        try:
            import pdfplumber
            import io
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            warnings.append("pdfplumber not installed — cannot parse PDF")
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=422,
                detail=f"Could not parse {file.filename} as PDF: {e}",
            )
    else:
        text = content.decode("utf-8", errors="replace")

    if not text.strip():
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Could not extract text from file")

    # Parse using the LLM agent
    from ...agents.requirement_intel import RequirementIntelAgent
    client = request.app.state.anthropic
    agent = RequirementIntelAgent(client)
    requirements = await agent.parse_document(text, source_type)

    req_dicts = [agent._to_dict(r) for r in requirements]

    # ── Preview mode: return parsed results without committing ─────────
    if preview:
        return {
            "preview": True,
            "requirement_ids": [r.id for r in requirements],
            "parsed_count": len(requirements),
            "source_type": source_type,
            "filename": file.filename,
            "warnings": warnings,
            "requirements": req_dicts,
        }

    # ── Change detection: upsert with hash comparison ────────────────────
    from ..db_adapter import try_db

    created, modified, unchanged = 0, 0, 0

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementRepo
            repo = RequirementRepo(db)
            for r, rd in zip(requirements, req_dicts):
                content_hash = RequirementIntelAgent.compute_content_hash(
                    r.title,
                    r.description,
                    rd.get("acceptance_criteria", []),
                )
                req_data = {
                    "req_id": r.id,
                    "title": r.title,
                    "description": r.description,
                    "source_type": source_type if source_type not in ("auto", "word", "excel", "pdf", "markdown", "text", "json", "yaml") else "plain_text",
                    "acceptance_criteria_snapshot": rd.get("acceptance_criteria", []),
                }
                if project_id:
                    import uuid as _uuid
                    req_data["project_id"] = _uuid.UUID(project_id)
                _, change_type = await repo.upsert_with_change_detection(
                    req_data, content_hash,
                )
                if change_type == "created":
                    created += 1
                elif change_type == "modified":
                    modified += 1
                else:
                    unchanged += 1

    return {
        "requirement_ids": [r.id for r in requirements],
        "parsed_count": len(requirements),
        "created": created,
        "modified": modified,
        "unchanged": unchanged,
        "source_type": source_type,
        "filename": file.filename,
        "warnings": warnings,
        "requirements": req_dicts,
    }


# ── Requirement Versioning Endpoints ─────────────────────────────────────


@router.get("/{req_id}/versions", dependencies=[Depends(_require_api_key)])
async def list_requirement_versions(req_id: str):
    """List all version snapshots for a requirement."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementVersionRepo, _to_dict
            repo = RequirementVersionRepo(db)
            versions = await repo.list_versions(req_id.upper())
            return {
                "req_id": req_id.upper(),
                "versions": [_to_dict(v) for v in versions],
                "total": len(versions),
            }

    return {"req_id": req_id.upper(), "versions": [], "total": 0}


@router.get("/{req_id}/versions/{v1}/diff/{v2}", dependencies=[Depends(_require_api_key)])
async def diff_requirement_versions(req_id: str, v1: int, v2: int):
    """Compute a unified diff between two requirement versions."""
    import difflib
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementVersionRepo
            repo = RequirementVersionRepo(db)
            ver_a = await repo.get_version(req_id.upper(), v1)
            ver_b = await repo.get_version(req_id.upper(), v2)
            if not ver_a or not ver_b:
                raise HTTPException(status_code=404, detail="One or both versions not found")

            # Build text blocks for diffing
            def _version_text(v) -> list[str]:
                lines = [
                    f"Title: {v.title_snapshot or ''}",
                    f"Description: {v.description_snapshot or ''}",
                    f"Hash: {v.content_hash}",
                ]
                for ac in (v.ac_snapshot or []):
                    lines.append(f"  AC {ac.get('id', '?')}: {ac.get('statement', '')}")
                return lines

            diff = list(difflib.unified_diff(
                _version_text(ver_a),
                _version_text(ver_b),
                fromfile=f"v{v1}",
                tofile=f"v{v2}",
                lineterm="",
            ))
            return {
                "req_id": req_id.upper(),
                "from_version": v1,
                "to_version": v2,
                "diff": "\n".join(diff),
                "changed": len(diff) > 0,
            }

    raise HTTPException(status_code=503, detail="Database not available")


# ── Jira / Confluence Ingestion ─────────────────────────────────────────


class JiraImportRequest(BaseModel):
    project_id: str | None = None          # attach imported reqs to this project
    sprint_id: str | None = None
    epic_key: str | None = None
    jql_filter: str | None = None
    keys: list[str] | None = None          # explicit issue keys (e.g. ["OP-123", ...])
    enrich_github: bool = True             # prepend the SUT repo route context
    synthesize_acs: bool = True            # run RequirementIntelAgent for ACs
    read_side_only: bool = True            # R154 wave-1 framing in AC prompt
    max_issues: int = 50


class ConfluenceImportRequest(BaseModel):
    page_id: str | None = None
    space_key: str | None = None


async def _build_project_jira_client(project: dict):
    """Build a per-project JiraClient from `project.integrations` (native
    per-SUT Jira). Falls back to the global env client only when the project
    carries no Jira credentials. Returns a connected client or None."""
    from ...integrations.jira_client import JiraClient
    integ = (project or {}).get("integrations") or {}
    url = integ.get("jira_url"); email = integ.get("jira_email")
    token = integ.get("jira_api_token"); key = integ.get("jira_project")
    if url and email and token:
        client = JiraClient(url=url, email=email, api_token=token, project_key=key or "PROJ")
        if await client.connect():
            return client
        log.warning("import/jira: per-project Jira connect failed for %s", (project or {}).get("id"))
    return None


@router.post("/import/jira", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def import_from_jira(body: JiraImportRequest, request: Request):
    """Natively import Jira issues as requirements WITH acceptance criteria,
    enriched from linked tickets + comments + (optionally) the SUT's GitHub code.

    Unlike the legacy path (raw title+desc, no ACs, no project scope), this:
      1. builds a PER-PROJECT JiraClient from project.integrations,
      2. enriches each issue with issuelinks + subtasks + comments (author
         context / root causes — the real AC material),
      3. synthesizes measurable given/when/then ACs via RequirementIntelAgent,
      4. attaches `project_id` and persists the ACs (like /bulk).
    """
    from ...agents.requirement_intel import RequirementIntelAgent, SourceType

    project = None
    jira = None
    if body.project_id:
        from .projects import _resolve_project
        project = await _resolve_project(body.project_id)
        if not project:
            raise HTTPException(404, f"Project {body.project_id} not found")
        jira = await _build_project_jira_client(project)
    if jira is None:  # fall back to the global env client
        jira = getattr(request.app.state, "jira", None)
    if not jira or not jira.available:
        raise HTTPException(503, "Jira not configured — set project.integrations.jira_url/"
                                 "jira_email/jira_api_token (or global JIRA_* env).")

    # 1) resolve the set of issue keys to import.
    if body.keys:
        keys = body.keys[: body.max_issues]
    else:
        if body.jql_filter:
            stories = await jira.search_issues(body.jql_filter, max_results=body.max_issues)
        else:
            stories = await jira.fetch_stories(body.sprint_id, body.epic_key)
        keys = [s.get("key") or s.get("id") for s in stories][: body.max_issues]
    keys = [k for k in keys if k]
    if not keys:
        raise HTTPException(404, "No Jira issues matched the given scope.")

    # 2) optional shared GitHub route-context prefix (grounds ACs in real
    #    endpoints). Reuses the agent-owned GitHub source fetch (R104.B).
    gh_context = ""
    if body.enrich_github and project:
        try:
            import asyncio as _aio
            from ...agents.github_context import fetch_code_context
            # Best-effort + bounded: the GitHub MCP fetch can hang (observed
            # blocking the whole import indefinitely). Cap it so enrichment is
            # a bonus, never a blocker — Jira links+comments carry the ACs.
            gh_context = ((await _aio.wait_for(fetch_code_context(project), timeout=25)) or "")[:3000]
        except _aio.TimeoutError:
            log.warning("import/jira: github enrichment timed out (25s) — proceeding without it")
        except Exception as exc:
            log.debug("import/jira: github enrichment skipped: %s", exc)

    client = request.app.state.anthropic
    agent = RequirementIntelAgent(client)
    _read_note = (
        "\n\n[TEST-DESIGN NOTE] Wave-1 is READ-SIDE only (non-mutating): frame "
        "acceptance criteria around viewing/listing/searching/report/status and "
        "GET API contracts. Tag write-path ACs as wave-2." if body.read_side_only else ""
    )

    # Incremental + resumable persistence: AC synthesis routes through the LLM
    # (claude_code ≈ 2 min/issue), so a 17-issue import runs ~40 min — longer
    # than any HTTP timeout. Persist EACH issue as it completes (not all-at-end)
    # so a disconnect/timeout keeps partial progress, and SKIP issues that
    # already carry ACs so a re-run resumes instead of redoing.
    async def _persist_one(req: dict) -> None:
        if not body.project_id:
            return
        bucket = PROJECT_REQUIREMENTS.setdefault(body.project_id, [])
        _ex = next((r for r in bucket if r.get("id") == req["id"]), None)
        if _ex:
            bucket[bucket.index(_ex)] = req
        else:
            bucket.append(req)
        _save_requirements()
        try:
            from ..db_adapter import try_db
            async with try_db() as db:
                if db:
                    from ...db.repository import RequirementRepo
                    import uuid as _uuid
                    _row = {
                        "req_id": req["id"], "title": req["title"],
                        "description": req["description"], "priority": req["priority"],
                        "acceptance_criteria": req["acceptance_criteria"],
                        "project_id": _uuid.UUID(body.project_id),
                    }
                    # R257 (WS2e) — carry the JIRA source into the DB too.
                    # `source_url` and `metadata_` ALREADY exist on the
                    # Requirement model (models.py) — no migration needed.
                    if req.get("source_url"):
                        _row["source_url"] = req["source_url"]
                    if req.get("metadata"):
                        _row["metadata_"] = req["metadata"]
                    await RequirementRepo(db).create(_row)
                    await db.commit()
        except Exception as exc:
            log.warning("import/jira: DB persist failed for %s: %s", req["id"], exc)

    _existing_with_acs = {
        r.get("id") for r in PROJECT_REQUIREMENTS.get(body.project_id or "", [])
        if (r.get("acceptance_criteria") or [])
    }

    imported: list[dict] = []
    per_issue: list[dict] = []
    for key in keys:
        if body.synthesize_acs and key in _existing_with_acs:
            per_issue.append({"key": key, "status": "skipped_has_acs"})
            continue
        try:
            enriched = await jira.fetch_issue_enriched(key)
        except Exception as exc:
            log.warning("import/jira: enrich failed for %s: %s", key, exc)
            per_issue.append({"key": key, "status": "fetch_failed", "error": str(exc)[:120]})
            continue
        text = enriched["enriched_text"]
        if gh_context:
            text += "\n\n## SUT source context (real routes/validations)\n" + gh_context
        text += _read_note

        acs: list[dict] = []
        title = enriched["summary"] or key
        desc = enriched["enriched_text"][:2000]
        if body.synthesize_acs:
            try:
                structured = await agent.parse_document(text, SourceType.JIRA_TICKET.value)
                if structured:
                    sr = structured[0]
                    title = sr.title or title
                    desc = sr.description or desc
                    acs = [
                        # Prefix ac.id with the issue key so ac_id is GLOBALLY
                        # unique — parse_document numbers ACs per-document
                        # (AC-001-01 …), which collides across issues on the
                        # DB's `acceptance_criteria.ac_id` UNIQUE constraint
                        # (ON CONFLICT DO NOTHING silently dropped all but the
                        # first issue's ACs → 0 ACs in the Test Architecture UI).
                        {"id": f"{key}-{ac.id}", "statement": ac.statement, "given": ac.given,
                         "when": ac.when, "then": ac.then, "covered": False,
                         "coverage_level": "NONE", "measurable": ac.measurable}
                        for ac in (sr.acceptance_criteria or [])
                    ]
            except Exception as exc:
                log.warning("import/jira: AC synthesis failed for %s: %s", key, exc)

        _prio = {
            "Highest": "P0", "High": "P1", "Medium": "P2",
            "Low": "P3", "Lowest": "P3",
        }.get(enriched.get("priority") or "", "P2")
        # R257 (WS2e) — PERSIST the JIRA ground truth.
        #
        # `desc` above is a lossy derivative: enriched_text truncated to 2000
        # chars, then OVERWRITTEN by the LLM's paraphrase (`sr.description`).
        # The issue's actual text — the requirement ARTA is supposed to be
        # testing — was never stored anywhere, so the ATDD designer could only
        # ever see a paraphrase of a truncation. BMAD TEA Layer 1 asks for
        # requirements grounded in the source; ARTA was grounding in its own
        # summary of it.
        #
        # `desc` KEEPS the paraphrase (downstream display contract is
        # unchanged); the source is added ALONGSIDE it. No schema change —
        # models.py already has Requirement.source_url + metadata_ (JSONB).
        # Killswitch ARTA_R257_JIRA_PERSIST_DISABLE=1.
        req = {
            "id": key, "req_id": key,
            "title": title,
            "description": desc,
            "priority": _prio,
            "source_type": "jira",
            "status": "approved",
            "project_id": body.project_id,
            "acceptance_criteria": acs,
            "entities": enriched.get("components") or [],
            "constraints": [f"jira:{key}"] + [f"link:{c}" for c in enriched.get("labels", [])[:5]],
            "coverage_pct": 0.0, "test_count": 0, "ac_count": len(acs),
        }
        if os.environ.get("ARTA_R257_JIRA_PERSIST_DISABLE") != "1":
            try:
                # R310 — JiraClient host is `.url` not `.base_url` (old getattr → empty jira_url).
                _jira_base = (getattr(jira, "url", "") or getattr(jira, "base_url", "") or "").rstrip("/")
                _jira_url = f"{_jira_base}/browse/{key}" if _jira_base else ""
                req["jira_key"] = key
                req["jira_url"] = _jira_url
                req["source_url"] = _jira_url
                req["metadata"] = {
                    "jira": {
                        # The UNtruncated, UNparaphrased issue text.
                        "enriched_text": enriched.get("enriched_text") or "",
                        "summary": enriched.get("summary") or "",
                        "priority": enriched.get("priority") or "",
                        "components": enriched.get("components") or [],
                        "labels": enriched.get("labels") or [],
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            except Exception as _r257_exc:
                log.warning("R257: JIRA source persist failed for %s: %s", key, _r257_exc)
        imported.append(req)
        await _persist_one(req)   # incremental — survives disconnect/timeout
        per_issue.append({"key": key, "status": "imported", "ac_count": len(acs),
                          "components": enriched.get("components")})

    return {
        "source": "jira",
        "project_id": body.project_id,
        "requirement_ids": [r["id"] for r in imported],
        "total": len(keys),
        "imported": len(imported),
        "with_acs": sum(1 for r in imported if r["acceptance_criteria"]),
        "enriched": {"github": bool(gh_context), "links_comments": True},
        "per_issue": per_issue[:60],
    }


@router.post("/import/confluence", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def import_from_confluence(body: ConfluenceImportRequest, request: Request):
    """Import requirements from Confluence pages with change detection."""
    confluence = getattr(request.app.state, "confluence", None)
    if not confluence or not confluence.available:
        raise HTTPException(503, "Confluence integration not configured — set CONFLUENCE_URL/EMAIL/API_TOKEN")

    pages: list[dict] = []
    if body.page_id:
        pages = [await confluence.get_page(body.page_id)]
    elif body.space_key:
        pages = await confluence.get_space_pages(body.space_key)
    else:
        raise HTTPException(400, "Provide either page_id or space_key")

    from ..db_adapter import try_db
    from ...agents.requirement_intel import RequirementIntelAgent

    client = getattr(request.app.state, "anthropic", None)
    agent = RequirementIntelAgent(client)

    total_created, total_modified, total_unchanged = 0, 0, 0
    all_req_ids: list[str] = []

    for page in pages:
        text = f"# {page['title']}\n\n{page['body']}"
        requirements = await agent.parse_document(text, "confluence")

        async with try_db() as db:
            if db:
                from ...db.repository import RequirementRepo
                repo = RequirementRepo(db)
                for r in requirements:
                    rd = agent._to_dict(r)
                    content_hash = RequirementIntelAgent.compute_content_hash(
                        r.title, r.description, rd.get("acceptance_criteria", [])
                    )
                    req_data = {
                        "req_id": r.id,
                        "title": r.title,
                        "description": r.description,
                        "source_type": "confluence",
                    }
                    _, change_type = await repo.upsert_with_change_detection(req_data, content_hash)
                    all_req_ids.append(r.id)
                    if change_type == "created":
                        total_created += 1
                    elif change_type == "modified":
                        total_modified += 1
                    else:
                        total_unchanged += 1

    return {
        "source": "confluence",
        "pages_processed": len(pages),
        "requirement_ids": all_req_ids,
        "total": len(all_req_ids),
        "created": total_created,
        "modified": total_modified,
        "unchanged": total_unchanged,
    }


# ── Manual Correction & Change Log ───────────────────────────────────────


class AcceptanceCriterionPatch(BaseModel):
    id: str
    title: str


class RequirementPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    acceptance_criteria: Optional[list[AcceptanceCriterionPatch]] = None


@router.patch("/{req_id}", dependencies=[Depends(_require_api_key), Depends(require_role("admin", "test_architect", "qa_lead"))])
async def correct_requirement(req_id: str, body: RequirementPatch):
    """Correct AI-extracted requirement fields and create a version snapshot.

    R55.6 — when ACs change, auto-queue regen markers for every test
    that traces back to a changed AC. Tests with an explicit `ac_id`
    linkage queue only when THEIR AC changed; tests with `ac_id=null`
    (e.g., pytest analytics layers) queue when any AC of the same
    requirement changed. R42.6 consumer drains the markers within
    5 min; next run uses regenerated specs that reflect the new AC.
    """
    from ..db_adapter import try_db

    # R55.6 — capture old ACs BEFORE we apply the patch so the delta
    # is computable. Both code paths (DB + mock) need this.
    old_acs: list = []

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementRepo, RequirementVersionRepo, _to_dict
            repo = RequirementRepo(db)
            existing = await repo.get(req_id.upper())
            if not existing:
                raise HTTPException(status_code=404, detail=f"Requirement {req_id} not found")

            # R55.6 — snapshot existing ACs before mutation
            try:
                old_acs = list(getattr(existing, "acceptance_criteria", None) or [])
            except Exception:
                old_acs = []

            # Apply patches
            update_data: dict = {}
            if body.title is not None:
                update_data["title"] = body.title
            if body.description is not None:
                update_data["description"] = body.description
            if body.priority is not None:
                update_data["priority"] = body.priority

            ac_snapshot = None
            if body.acceptance_criteria is not None:
                ac_snapshot = [{"id": ac.id, "title": ac.title} for ac in body.acceptance_criteria]

            # Update the requirement row
            for key, value in update_data.items():
                setattr(existing, key, value)
            await db.flush()

            # Create a version snapshot recording the manual correction
            version_repo = RequirementVersionRepo(db)
            from ...agents.requirement_intel import RequirementIntelAgent
            content_hash = RequirementIntelAgent.compute_content_hash(
                existing.title,
                existing.description,
                ac_snapshot or [],
            )
            await version_repo.create_version(
                req_id=req_id.upper(),
                title_snapshot=existing.title,
                description_snapshot=existing.description,
                ac_snapshot=ac_snapshot,
                content_hash=content_hash,
                change_type="manual_correction",
            )
            await db.commit()

            d = _to_dict(existing)
            if ac_snapshot is not None:
                d["acceptance_criteria"] = ac_snapshot

            # R55.6 — queue regen markers if ACs changed
            regen_queued = _r55_6_queue_regen_on_ac_change(
                req_id=req_id.upper(),
                old_acs=old_acs,
                new_acs=ac_snapshot,
            )
            d["regen_queued"] = regen_queued
            return d

    # Mock fallback — apply patches in-memory (search all project stores)
    req = None
    for reqs in PROJECT_REQUIREMENTS.values():
        req = next((r for r in reqs if r["id"] == req_id.upper()), None)
        if req:
            break
    if not req:
        raise HTTPException(status_code=404, detail=f"Requirement {req_id} not found")

    # R55.6 — snapshot old ACs before mutation
    old_acs = list(req.get("acceptance_criteria") or [])

    updated = dict(req)
    if body.title is not None:
        updated["title"] = body.title
    if body.description is not None:
        updated["description"] = body.description
    if body.priority is not None:
        updated["priority"] = body.priority

    new_acs_for_marker = None
    if body.acceptance_criteria is not None:
        new_acs_for_marker = [
            {"id": ac.id, "title": ac.title} for ac in body.acceptance_criteria
        ]
        updated["acceptance_criteria"] = new_acs_for_marker
        updated["ac_count"] = len(updated["acceptance_criteria"])

    # R55.6 — queue regen markers if ACs changed
    regen_queued = _r55_6_queue_regen_on_ac_change(
        req_id=req_id.upper(),
        old_acs=old_acs,
        new_acs=new_acs_for_marker,
    )
    updated["regen_queued"] = regen_queued
    return updated


def _r55_6_queue_regen_on_ac_change(
    req_id: str, old_acs: list, new_acs: list | None,
) -> int:
    """R55.6 — compute AC delta, write a regen marker per affected
    test. Returns the count of markers written.

    Marker shape matches R55.3 / R57.1 so the R42.6 consumer's
    `_build_hint_block` formats it consistently:
      {test_id, triage_category=requirement_changed,
       signals=[ac_delta, AC-id-1, ...], sample_error, violation_details, ...}
    """
    if new_acs is None:
        return 0   # AC list not in the patch — no regen needed

    # Normalise both lists to {id: title} maps. R55.6 — `title` is
    # what AC dicts carry in this codebase (alias for statement); use
    # `statement` as a defensive fallback for older serialised forms.
    def _flatten(acs: list) -> dict[str, str]:
        out: dict[str, str] = {}
        for ac in acs or []:
            if isinstance(ac, dict):
                aid = ac.get("id") or ""
                text = ac.get("title") or ac.get("statement") or ""
                if aid:
                    out[aid] = text
            elif hasattr(ac, "id"):
                out[getattr(ac, "id", "") or ""] = (
                    getattr(ac, "title", "") or getattr(ac, "statement", "") or ""
                )
        return out

    old_map = _flatten(old_acs)
    new_map = _flatten(new_acs)
    changed_ac_ids = sorted(
        aid for aid in (set(old_map) | set(new_map))
        if old_map.get(aid) != new_map.get(aid)
    )
    if not changed_ac_ids:
        return 0

    try:
        from .tests_state import GENERATED_TESTS
    except Exception as exc:
        log.warning("R55.6: GENERATED_TESTS import failed: %s", exc)
        return 0

    from pathlib import Path as _Path
    from datetime import datetime as _dt, timezone as _tz
    import json as _json
    marker_dir = _Path(".arta/regen_queue")
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("R55.6: marker dir create failed: %s", exc)
        return 0

    queued = 0
    for t in GENERATED_TESTS:
        if (t.get("requirement_id") or "").upper() != req_id:
            continue

        # Per-test AC linkage: GENERATED_TESTS entries have `ac_id`
        # (single) or `ac_ids` (list, rare). Tests with null ac_id
        # are not bound to a specific AC — queue on ANY change.
        t_ac_id = t.get("ac_id")
        t_ac_ids = t.get("ac_ids") or ([t_ac_id] if t_ac_id else [])
        if t_ac_ids and not any(aid in changed_ac_ids for aid in t_ac_ids if aid):
            continue

        tid = t.get("id") or t.get("test_id")
        if not tid:
            continue
        affected_acs = (
            [aid for aid in t_ac_ids if aid in changed_ac_ids] or changed_ac_ids
        )
        marker = {
            "test_id": tid,
            "triage_category": "requirement_changed",
            "signals": ["ac_delta", *affected_acs[:3]],
            "sample_error": f"AC(s) changed: {', '.join(affected_acs[:3])}",
            "violation_details": [
                {
                    "ac_id": aid,
                    "old": old_map.get(aid, ""),
                    "new": new_map.get(aid, ""),
                }
                for aid in affected_acs[:10]
            ],
            "queued_at": _dt.now(_tz.utc).isoformat(),
            "queued_by": "R55.6_requirement_patch",
        }
        try:
            (marker_dir / f"{tid}.json").write_text(_json.dumps(marker, indent=2))
            queued += 1
        except OSError as exc:
            log.warning("R55.6: marker write failed for %s: %s", tid, exc)

    if queued:
        log.info(
            "R55.6: requirement %s patched — %d AC(s) changed, %d test(s) queued for regen",
            req_id, len(changed_ac_ids), queued,
        )
    return queued


MOCK_CHANGE_LOG = [
    {
        "version": 3,
        "changed_at": "2026-03-14T10:30:00Z",
        "changed_by": "qa_lead",
        "trigger": "manual_correction",
        "summary": "Updated acceptance criteria wording for clarity",
        "diff": "- AC-001: Valid payment processes within 3s\n+ AC-001: Valid payment completes within 3 seconds with status 200",
    },
    {
        "version": 2,
        "changed_at": "2026-03-13T15:45:00Z",
        "changed_by": "system",
        "trigger": "re_upload",
        "summary": "Description updated from Jira sync",
        "diff": "- description: Handle checkout payments\n+ description: Handle checkout payments including 3DS verification",
    },
    {
        "version": 1,
        "changed_at": "2026-03-12T09:00:00Z",
        "changed_by": "system",
        "trigger": "initial_upload",
        "summary": "Requirement first ingested from uploaded PRD",
        "diff": "",
    },
]


@router.get("/{req_id}/changes", dependencies=[Depends(_require_api_key)])
async def get_requirement_changes(req_id: str):
    """Return the change log for a requirement, newest-first."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import RequirementVersionRepo, _to_dict
            import difflib

            repo = RequirementVersionRepo(db)
            versions = await repo.list_versions(req_id.upper())
            if not versions:
                return {"req_id": req_id.upper(), "changes": [], "total": 0}

            changes: list[dict] = []
            for i, v in enumerate(versions):
                diff_text = ""
                if i < len(versions) - 1:
                    prev = versions[i + 1]  # versions are newest-first

                    def _lines(ver) -> list[str]:
                        lines = [
                            f"Title: {ver.title_snapshot or ''}",
                            f"Description: {ver.description_snapshot or ''}",
                        ]
                        for ac in (ver.ac_snapshot or []):
                            lines.append(f"  AC {ac.get('id', '?')}: {ac.get('statement', ac.get('title', ''))}")
                        return lines

                    diff_text = "\n".join(difflib.unified_diff(
                        _lines(prev), _lines(v),
                        fromfile=f"v{prev.version_number}",
                        tofile=f"v{v.version_number}",
                        lineterm="",
                    ))

                vd = _to_dict(v)
                changes.append({
                    "version": vd.get("version_number", i + 1),
                    "changed_at": str(vd.get("created_at", "")),
                    "changed_by": vd.get("changed_by", "system"),
                    "trigger": vd.get("change_type", "unknown"),
                    "summary": vd.get("change_summary", f"Version {vd.get('version_number', i + 1)}"),
                    "diff": diff_text,
                })

            return {"req_id": req_id.upper(), "changes": changes, "total": len(changes)}

    # Mock fallback
    return {"req_id": req_id.upper(), "changes": MOCK_CHANGE_LOG, "total": len(MOCK_CHANGE_LOG)}


# ── Sync seed requirements to DB on startup ──────────────────────────────────

async def _sync_requirements_to_db(app):
    """Insert ALL in-memory requirements into PostgreSQL on startup
    (idempotent, ON CONFLICT DO NOTHING).

    F20-7: Previously this only synced BUGTRACKR_REQUIREMENTS, leaving
    PROJECT_REQUIREMENTS for other demo projects (Analytics Demo, E-Commerce, etc.)
    in memory only. The downstream effect: every test persisted for those
    projects got NULL `requirement_id` because the test-persist subselect
    `(SELECT id FROM requirements WHERE req_id=:rid AND project_id=:pid)`
    returned NULL (no matching row in the requirements table). That broke
    test↔requirement traceability and caused the F20-4 hydration band-aid.

    Now we walk every (project_id, [reqs]) entry in PROJECT_REQUIREMENTS
    and persist all of them. After this runs, the test-persist subselect
    succeeds for every project that has in-memory seed data, and new
    tests get a proper FK from day one.

    Failures are logged but never block startup — the F20-4 metadata
    hydration in /api/tests still recovers requirement_id at read time
    if the sync fails for a specific row.
    """
    from ...db.session import async_session_factory
    from sqlalchemy import text

    req_count = 0
    ac_count = 0
    proj_count = 0

    try:
        async with async_session_factory() as session:
            for default_project_id, reqs_list in PROJECT_REQUIREMENTS.items():
                proj_count += 1
                for req in reqs_list:
                    req_id_text = req.get("req_id") or req.get("id")
                    if not req_id_text:
                        continue
                    # Each req may carry its own project_id; if missing,
                    # use the dict-key project_id as the default.
                    req_project_id = req.get("project_id") or default_project_id

                    # Map 'manual' source_type to 'plain_text' (enum-safe)
                    source = req.get("source_type", "plain_text")
                    if source not in ("jira", "github_issue", "confluence",
                                      "openapi", "plain_text", "db_schema"):
                        source = "plain_text"

                    # Clamp risk_score to integer 1-9 for the DB column constraint
                    raw_risk = req.get("risk_score")
                    risk_int = max(1, min(9, int(raw_risk))) if raw_risk is not None else None

                    try:
                        await session.execute(text("""
                            INSERT INTO requirements
                                (req_id, title, description, priority, risk_score, impact, probability,
                                 source_type, status, project_id)
                            VALUES
                                (:req_id, :title, :description,
                                 CAST(:priority AS risk_priority), :risk_score, :impact, :probability,
                                 CAST(:source AS source_type), CAST(:status AS req_status), CAST(:project_id AS uuid))
                            ON CONFLICT (req_id) DO UPDATE
                                SET project_id = EXCLUDED.project_id,
                                    title = EXCLUDED.title,
                                    description = EXCLUDED.description,
                                    priority = EXCLUDED.priority,
                                    risk_score = COALESCE(EXCLUDED.risk_score, requirements.risk_score)
                                WHERE requirements.project_id IS NULL
                                   OR requirements.project_id = EXCLUDED.project_id
                        """), {
                            "req_id": req_id_text,
                            "title": req.get("title", ""),
                            "description": req.get("description", ""),
                            "priority": req.get("priority", "P2"),
                            "risk_score": risk_int,
                            "impact": req.get("impact"),
                            "probability": req.get("probability"),
                            "source": source,
                            "status": req.get("status", "draft"),
                            "project_id": req_project_id,
                        })
                        req_count += 1
                    except Exception as exc:
                        log.warning("sync: failed to upsert requirement %s for project %s: %s",
                                    req_id_text, req_project_id, exc)
                        continue

                    # Fetch the UUID `id` for this requirement (needed as FK for ACs)
                    row = (await session.execute(
                        text("SELECT id FROM requirements WHERE req_id = :req_id"),
                        {"req_id": req_id_text},
                    )).fetchone()
                    if not row:
                        continue
                    req_uuid = row[0]

                    for ac in req.get("acceptance_criteria", []):
                        ac_id_val = ac.get("id") or ac.get("ac_id")
                        if not ac_id_val:
                            continue
                        try:
                            await session.execute(text("""
                                INSERT INTO acceptance_criteria
                                    (ac_id, requirement_id, title, given, when_, then_, priority)
                                VALUES
                                    (:ac_id, :req_uuid, :title, :given, :when_, :then_,
                                     CAST(:priority AS risk_priority))
                                ON CONFLICT (ac_id) DO NOTHING
                            """), {
                                "ac_id": ac_id_val,
                                "req_uuid": req_uuid,
                                "title": ac.get("statement", "") or ac.get("title", ""),
                                "given": ac.get("given", ""),
                                "when_": ac.get("when", ""),
                                "then_": ac.get("then", ""),
                                "priority": req.get("priority", "P2"),
                            })
                            ac_count += 1
                        except Exception as exc:
                            log.warning("sync: failed to upsert AC %s for req %s: %s",
                                        ac_id_val, req_id_text, exc)

            await session.commit()
            log.info("Synced %d requirements and %d ACs across %d projects to DB",
                     req_count, ac_count, proj_count)

    except Exception as exc:
        log.warning("Could not sync requirements to DB: %s", exc)
