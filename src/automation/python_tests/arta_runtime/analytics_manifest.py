"""ARTA Analytics Workflow Manifest — the SUT-AGNOSTIC seam for the analytics chain.

The mission-generic insight (from mapping a real analytics SUT's source, but true of
any analytics SUT): the WORKFLOW SHAPE is invariant —

    mint-scoped-token → [upload artifact → register artifact]* → create-dataset
      → TRIGGER-INGESTION(job) → poll-until-ready → (optional) create-app → query → stream

…while everything SUT-SPECIFIC (hosts, auth per host, id/segment values, the set of
dataset-creation MODES, endpoint path templates, the dataset_type→engine routing) is
DATA. This module holds that data as a manifest so a GENERIC executor
(`analytics_workflow.py`) runs ANY SUT. `ingestion` reads its hosts/endpoints/
modes from here instead of hardcoding them.

The default manifest below is authored from a reference SUT's backend request schemas
(create-data-set `CreateUserDataset`, create-app `CreateAppData`, media/cm handlers,
monitoring `JobDetails` job-create, and the query_router engine `queue_mapping`). It is
the DEFAULT; `load_manifest()` overlays `ARTA_AN_WORKFLOW_MANIFEST` (JSON) or the
project's `env_block.analytics`, so a future `discover_analytics_workflow_from_source`
(agent-owned MCP, the auth-chain pattern) can emit a fresh manifest per SUT with zero
code change. Individual values also keep their existing `ARTA_AN_*` env overrides.
"""
from __future__ import annotations

import copy
import json
import os

DEFAULT_MANIFEST: dict = {
    # hosts: each with a base (env override → default) + the auth token kind to use.
    "hosts": {
        "backend": {"env": "TARGET_AUTH_MINT_BASE", "default": "https://backend.app.example.internal",
                    "auth": "session_token"},      # media + cm verify the raw session token (signature)
        "analytics": {"env": "TARGET_ANALYTICS_BASE_URL",
                      "default": "https://api.app.example.internal/analytics/v1",
                      "auth": "agent_user_token"},  # create-dataset / status / app / query
        "monitor": {"env": "ARTA_AN_MONITOR_BASE", "default": "https://monitoring.app.example.internal",
                    "auth": "agent_user_token"},    # job-create ingestion trigger
    },
    # identifiers: SUT/deployment-specific segment values (env → default → token claims).
    "identifiers": {
        "schema_id": {"env": "ARTA_AN_COLLECTIONS_SCHEMA", "default": "analytics_schema"},
    },
    "collections": {"files": "analytics_files", "datasets": "analytics_datasets",
                    "data_sources": "analytics_data_sources"},
    # dataset-creation MODES (the operator's four) — each yields a dataset_type that
    # routes to a specific engine (query_router.py queue_mapping). `verifies` tells the
    # generator which assertion classes this engine can answer.
    "dataset_modes": {
        "files": {"id_prefix": "files_", "dataset_type": "files", "engine": "document_rag",
                  "verifies": ["content"], "is_excel": False,
                  "upload_exts": [".txt", ".csv", ".pdf", ".md", ".mdx"], "flow": "upload"},
        # excel seeds via the monitoring `job-create` path (same as files) — the resulting
        # user_master record is structurally identical to a working SPA excel dataset
        # the earlier ReadTimeout/"No files found" were CLIENT-side query defects (R218.AM.4
        # read-timeout + AM.5 source_type), not ingestion. Proven live: count==42/sum==6321.
        "excel": {"id_prefix": "excel_", "dataset_type": "excel", "engine": "tabular",
                  "verifies": ["count", "aggregation", "content"], "is_excel": True,
                  "upload_exts": [".xlsx"], "flow": "upload"},
        "mongo": {"id_prefix": "mongo_", "dataset_type": "mongo", "engine": "tabular",
                  "verifies": ["count", "aggregation", "content"], "flow": "register"},
    },
    # endpoint path templates (placeholders filled from ids). host → which host block.
    "endpoints": {
        "presign_upload": {"host": "backend", "method": "POST",
            "path": "/api/media/{account}/subscriber/{subscriber}/subscription/{subscription}/{schema}/generate-upload-url"},
        "download_url": {"host": "backend", "method": "GET",
            "path": "/api/media/{account}/subscriber/{subscriber}/subscription/{subscription}/{schema}/generate-upload-url"},
        "register_file": {"host": "backend", "method": "POST",
            "path": "/{account}/api/collection/{account}/user/private/cm/v1/{schema}/{files_collection}"},
        "create_dataset": {"host": "analytics", "method": "POST",
            "path": "/subscriber/{subscriber}/subscription/{subscription}/function/user-management/event/create-data-set"},
        "trigger_job": {"host": "monitor", "method": "POST",
            "path": "/subscriber_id/{subscriber}/subscription_id/{subscription}/account_id/{account}/project_id/{project}/function/monitoring/event/job-create"},
        # ★ the SUT's REAL ingestion-completion signal (live-proven 2026-07-23): the
        # monitoring service reports the consumer's terminal state (Success/Failed) keyed
        # by the job_id we send in job-create. UNLIKE job-create (which needs the full
        # subscriber/subscription/account/project prefix), job-STATUS is the BARE path
        # (OpenAPI-discovered + live 200). `dataset-status` below is a DATA-PROCESSING
        # projection that never advances for a seeded dataset — poll_status gates on THIS.
        "monitor_job_status": {"host": "monitor", "method": "GET",
            "path": "/function/monitoring/event/job-status"},
        "dataset_status": {"host": "analytics", "method": "GET",
            "path": "/subscriber/{subscriber}/subscription/{subscription}/function/data-processing/event/dataset-status"},
        "create_app": {"host": "analytics", "method": "POST",
            "path": "/subscriber/{subscriber}/subscription/{subscription}/function/user-management/event/create-app"},
        "list_apps": {"host": "analytics", "method": "GET",
            "path": "/subscriber/{subscriber}/subscription/{subscription}/function/user-management/event/get-list-of-apps"},
        "query": {"host": "analytics", "method": "POST",
            "path": "/subscriber/{subscriber}/subscription/{subscription}/function/query-engine/event/query"},
    },
    # verification-class → the dataset MODE whose engine can answer it. The generator
    # uses this so a "count" test seeds an excel/mongo dataset (tabular), never a
    # files/document dataset (which only answers content).
    "routing_rules": {"count": "excel", "aggregation": "excel", "content": "files"},
    # readiness signal for poll (dataset-status): the file-consumer populates `sources`.
    "readiness": {"ready_values": ["success", "ready", "completed", "complete", "indexed"],
                  "failed_values": ["failed"], "sources_field": "sources"},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_manifest() -> dict:
    """The active workflow manifest: DEFAULT overlaid with a discovered/operator manifest
    from `ARTA_AN_WORKFLOW_MANIFEST` (JSON). SUT-agnostic seam — a future source-discovery
    emits this JSON; today it's the source-grounded reference default."""
    raw = os.environ.get("ARTA_AN_WORKFLOW_MANIFEST")
    if raw:
        try:
            return _deep_merge(DEFAULT_MANIFEST, json.loads(raw))
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_MANIFEST)


# ── resolution helpers (generic; used by the executor) ────────────────────────
def host_base(manifest: dict, host_id: str) -> str:
    h = (manifest.get("hosts") or {}).get(host_id) or {}
    return (os.environ.get(h.get("env", "")) or h.get("default") or "").rstrip("/")


def host_auth_kind(manifest: dict, host_id: str) -> str:
    return ((manifest.get("hosts") or {}).get(host_id) or {}).get("auth", "agent_user_token")


def identifier(manifest: dict, name: str, fallback: str = "") -> str:
    spec = (manifest.get("identifiers") or {}).get(name)
    if isinstance(spec, dict):
        return os.environ.get(spec.get("env", ""), "") or spec.get("default", "") or fallback
    return fallback


def endpoint_url(manifest: dict, ep_id: str, subst: dict) -> tuple[str, str, str]:
    """Return (method, absolute_url, host_id) for an endpoint, filling {placeholders}
    from `subst` (missing keys left as-is so the miss is visible). Unknown → ("", "", "")."""
    ep = (manifest.get("endpoints") or {}).get(ep_id)
    if not ep:
        return "", "", ""
    base = host_base(manifest, ep.get("host", ""))
    path = ep.get("path", "")
    for k, v in (subst or {}).items():
        path = path.replace("{" + k + "}", str(v))
    return ep.get("method", "GET"), f"{base}{path}", ep.get("host", "")


def dataset_mode(manifest: dict, mode: str) -> dict:
    return (manifest.get("dataset_modes") or {}).get(mode) or {}


def mode_for_verification(manifest: dict, verify_class: str, *, default: str = "files") -> str:
    """Pick the dataset MODE whose engine can verify `verify_class` (count/aggregation/
    content). This is the rule that stops a 'count' test from seeding a document dataset."""
    rules = manifest.get("routing_rules") or {}
    mode = rules.get(verify_class)
    if mode and mode in (manifest.get("dataset_modes") or {}):
        return mode
    # else: first mode that lists the class in `verifies`
    for name, spec in (manifest.get("dataset_modes") or {}).items():
        if verify_class in (spec.get("verifies") or []):
            return name
    return default
