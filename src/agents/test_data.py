"""R211 Phase C — shared Test-DATA layer.

ONE preparation of request bodies / resource ids, consumed by ALL tool
generators (closes the per-tool asymmetry: body synthesis existed for Newman
via `contract_test_generator._example_for_schema` but Playwright had none).

`synthesize_body` REUSES the Newman synthesizer (import — do not reimplement)
and then fills session-id fields with REAL harvested ids (the same correction
R210 does for paths). `build_request_bodies` resolves the per-path body schema
from (1) OpenAPI requestBody → (2) captured request_body_shape → (3) source
`body_schema_source` (the only source populated for THIS SUT today — logged
when all are empty so the gap is visible, not silent).

Honest constraint (measured): this SUT's OpenAPI has 0 requestBody and captured
endpoints have 0 request_body_shape, so synthesis no-ops until Phase F's
workflow-aware probe captures real bodies. The mechanism is built + tested so it
activates the moment a schema source exists.
"""
from __future__ import annotations

import json
import logging

from .contract_test_generator import _example_for_schema  # REUSE — single source

log = logging.getLogger("arta.test_data")

# Param-name → session-id key (kept in sync with
# `automation_engineer._R186_PARAM_TO_SESSION_ID`; inlined here to avoid pulling
# the heavy automation_engineer module into the shared test-data layer).
_R186_PARAM_TO_SESSION_ID = {
    "orgid": "organization_id", "org_id": "organization_id", "organizationid": "organization_id",
    "space_id": "workspace_id", "spaceid": "workspace_id", "space": "workspace_id",
    "workspaceid": "workspace_id", "workspace_id": "workspace_id",
    "project_id": "project_id", "projectid": "project_id",
    "subscriber_id": "subscriber_id", "subscription_id": "subscription_id",
    "account_id": "account_id",
}


def _fill_session_ids(value, known_ids: dict):
    """Recursively overwrite session-id-shaped fields with REAL harvested ids
    (account_id/subscriber_id/subscription_id/schema_id/organization_id/...).
    Mirrors R210's param-value correction, applied to a synthesized body."""
    if not known_ids:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            real = None
            if kl in known_ids:
                real = known_ids[kl]
            elif kl in _R186_PARAM_TO_SESSION_ID:
                real = known_ids.get(_R186_PARAM_TO_SESSION_ID[kl])
            if real and isinstance(v, (str, int, float)):
                out[k] = real
            else:
                out[k] = _fill_session_ids(v, known_ids)
        return out
    if isinstance(value, list):
        return [_fill_session_ids(v, known_ids) for v in value]
    return value


def synthesize_body(schema: dict | None, *, components: dict | None = None,
                    known_ids: dict | None = None) -> dict | None:
    """Synthesize a minimal valid JSON body from an OpenAPI requestBody schema
    (R115.A.1 required-only via `_example_for_schema`), then fill session-id
    fields with real harvested ids. Returns None when there's no schema."""
    if not schema:
        return None
    body = _example_for_schema(schema, components or {})
    if body is None:
        return None
    return _fill_session_ids(body, known_ids or {})


def _openapi_request_schema(op: dict, components: dict) -> dict | None:
    rb = (op or {}).get("requestBody") or {}
    content = (rb.get("content") or {}) if isinstance(rb, dict) else {}
    js = (content.get("application/json") or {}).get("schema") or {}
    return js or None


def load_captured_request_bodies(project_id: str | None) -> list[dict]:
    """R211 Phase F — load the would-be request bodies the discovery probe
    captured at the R154.A interceptor (aborted, R154-safe). Each entry is
    {method, url, postData}. Path: `.arta/discovered_request_bodies/<pid>.json`."""
    if not project_id:
        return []
    from pathlib import Path
    p = Path(".arta/discovered_request_bodies") / f"{project_id}.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _norm_body_path(url: str) -> str:
    import re as _re
    import urllib.parse as _up
    s = _up.unquote(url or "")
    s = _re.sub(r"^https?://[^/]+", "", s).split("?", 1)[0]
    if not s.startswith("/"):
        s = "/" + s
    return _re.sub(r"/+", "/", s).rstrip("/") or "/"


def build_request_bodies(
    *,
    openapi_spec: dict | None = None,
    captured_endpoints: list[dict] | None = None,
    captured_request_bodies: list[dict] | None = None,
    project_id: str | None = None,
    known_ids: dict | None = None,
) -> dict:
    """Build {`METHOD path` : synthesized_body} for every POST/PUT/PATCH the SUT
    exposes. Source precedence: (1) OpenAPI requestBody → (2) captured
    `request_body_shape` → (3) the Phase-F probe-captured REAL request bodies
    (`discovered_request_bodies/<pid>.json`, the only populated source for THIS
    SUT once a workflow-aware probe has run). Logs `r211_no_body_schema_source`
    when nothing is available so the gap stays visible (not silent)."""
    out: dict[str, dict] = {}
    components = ((openapi_spec or {}).get("components") or {})
    for path, methods in ((openapi_spec or {}).get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in ("post", "put", "patch"):
                continue
            schema = _openapi_request_schema(op, components)
            body = synthesize_body(schema, components=components, known_ids=known_ids)
            if body is not None:
                out[f"{method.upper()} {path}"] = body
    for e in (captured_endpoints or []):
        m = str((e or {}).get("method") or "").upper()
        p = (e or {}).get("path") or ""
        if m not in ("POST", "PUT", "PATCH") or not p:
            continue
        key = f"{m} {p}"
        if key in out:
            continue
        shape = (e or {}).get("request_body_shape")
        if shape:
            body = synthesize_body(shape, known_ids=known_ids)
            if body is not None:
                out[key] = body
    # (3) Phase-F probe-captured real bodies — the SPA actually built these, so
    # they are the highest-fidelity synthesis source. Use the real body, just
    # overwrite session-id fields with the live ids.
    crb = captured_request_bodies
    if crb is None and project_id:
        crb = load_captured_request_bodies(project_id)
    for rb in (crb or []):
        m = str((rb or {}).get("method") or "").upper()
        if m not in ("POST", "PUT", "PATCH"):
            continue
        key = f"{m} {_norm_body_path((rb or {}).get('url') or '')}"
        if key in out or key.endswith(" /"):
            continue
        try:
            parsed = json.loads((rb or {}).get("postData") or "")
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            out[key] = _fill_session_ids(parsed, known_ids or {})
    # (4) R297 — DTO-from-SUT-source bodies. The blocked families (Reefer/.NET)
    # publish no reachable OpenAPI and the probe never triggers their POSTs, so
    # sources (1)-(3) are empty for them — yet the AC names the DTO
    # (`CommandCenterRequest`) and the class lives in SUT source, reachable via
    # the agent-owned GitHub MCP. `dto_extractor.build_dto_request_bodies`
    # pre-fetches + persists those; read them here (sync) as the 4th source.
    # Lowest precedence: real observed/contract bodies always win over a
    # source-derived example. Session-id fields overwritten with live ids.
    if project_id:
        try:
            from .dto_extractor import load_dto_request_bodies
            for _k, _b in (load_dto_request_bodies(project_id) or {}).items():
                if _k not in out and isinstance(_b, dict) and _b:
                    out[_k] = _fill_session_ids(_b, known_ids or {})
        except Exception as _dto_exc:
            log.debug("R297: DTO-body source skipped: %s", _dto_exc)
    if not out:
        log.info("r211_no_body_schema_source: 0 request bodies synthesizable "
                 "(OpenAPI requestBody + captured request_body_shape + Phase-F "
                 "probe bodies all empty) — body synthesis no-ops until a "
                 "workflow-aware probe captures action bodies")
    return out


__all__ = ["synthesize_body", "build_request_bodies", "_fill_session_ids"]
