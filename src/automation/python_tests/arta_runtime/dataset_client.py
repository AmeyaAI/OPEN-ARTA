"""AN3 (R218) — SUT dataset-ingestion client (the write-side the manual-tester
correctness approach needs). Implements the AN3.0-DISCOVERED ingestion manifest
(learned from the SUT's frontend source, not guessed):

    upload_file  POST data-processing/event/file-upload   {file, file_name} -> file_id
    create_dataset POST user-management/event/create-data-set {name, dataset_type, file_id} -> dataset_id
    poll status  GET  data-processing/event/dataset-status {dataset_id} -> status
    delete       DELETE user-management/event/delete-data-set {dataset_id}

So ARTA can: generate known data -> LOAD it into the SUT -> query it -> verify the
SUT's insight against the INDEPENDENTLY-computed ground truth (AN1/recipe verifier)
-> tear the dataset down. This makes the analytics verdict trustworthy (the SUT
answers about data whose true properties we know).

A8 (R218) adds the APP layer the analytics query is SCOPED to (app_id): the SUT's
`get-list-of-apps` (GET, read-only) lets ARTA resolve a valid app_id WITHOUT a
write; `create-app` (write) is R154-gated. A null app_id is the query-422 root cause.

HARD R154 GATE: loading a dataset is a WRITE. It runs ONLY when the operator has
opted in: `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` AND `SUT_TEST_DATA_NAMESPACE=<id>`
(scoped sandbox). Otherwise `seed_required()` is False and the correctness flow
SKIPs to G2 read-only invariants (AN5). All dataset names are namespaced so cleanup
is scoped; teardown is GUARANTEED (the `seeded_dataset` context manager deletes in a
`finally`, even on failure — no orphaned SUT data).
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid

from . import analytics_backend as _ab

log = logging.getLogger("arta.analytics.dataset")

_INGEST_TIMEOUT = float(os.environ.get("ARTA_ANALYTICS_INGEST_TIMEOUT", "120"))
# function/event path segments (AN3.0-discovered; env-overridable if a SUT differs).
_EP = {
    "upload": os.environ.get("ARTA_AN_UPLOAD_EP", "data-processing/event/file-upload"),
    "create": os.environ.get("ARTA_AN_CREATE_EP", "user-management/event/create-data-set"),
    "status": os.environ.get("ARTA_AN_STATUS_EP", "data-processing/event/dataset-status"),
    "delete": os.environ.get("ARTA_AN_DELETE_EP", "user-management/event/delete-data-set"),
    # A8 (R218) — the APP layer the analytics query is SCOPED to. `get-list-of-apps`
    # is a GET (read-only) → app_id can be resolved WITHOUT a write. create/delete-app
    # are writes (R154-gated). Env-overridable per SUT (discovered, not hard-coded).
    "list_apps": os.environ.get("ARTA_AN_LIST_APPS_EP", "user-management/event/get-list-of-apps"),
    "create_app": os.environ.get("ARTA_AN_CREATE_APP_EP", "user-management/event/create-app"),
    "delete_app": os.environ.get("ARTA_AN_DELETE_APP_EP", "user-management/event/delete-app"),
}


def seed_required() -> tuple[bool, str]:
    """R154 gate. Returns (allowed, reason). Allowed ONLY with the explicit opt-in
    + a sandbox namespace — never by default (ARTA's non-mutation guarantee)."""
    if os.environ.get("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS") != "1":
        return False, "ARTA_R154_ALLOW_DESTRUCTIVE_TESTS!=1 (SUT write not permitted)"
    if not os.environ.get("SUT_TEST_DATA_NAMESPACE", "").strip():
        return False, "SUT_TEST_DATA_NAMESPACE unset (no sandbox scope)"
    return True, "ok"


def _namespace() -> str:
    return os.environ.get("SUT_TEST_DATA_NAMESPACE", "arta-test").strip()


class DatasetClient:
    """Seeds + tears down a namespaced test dataset in the SUT. Reuses the
    analytics client's session + DISCOVERED auth chain (Bearer tokenId)."""

    def __init__(self):
        self._ctx = None
        self._auth = _ab.AnalyticsClient()  # reuse _build_auth + _resolve_context

    def _context(self) -> dict:
        if self._ctx is None:
            self._ctx = _ab._resolve_context(_ab._load_storage())
        return self._ctx

    def _url(self, ctx: dict, ep_key: str) -> str:
        base, sid, subid = ctx["base_url"], ctx["subscriber_id"], ctx["subscription_id"]
        return f"{base}/subscriber/{sid}/subscription/{subid}/function/{_EP[ep_key]}"

    def _headers(self, ctx: dict, path: str, *, json_ct: bool = True) -> tuple[dict, bool]:
        headers, ok = self._auth._build_auth(path, ctx)
        if not json_ct:
            headers.pop("Content-Type", None)  # multipart sets its own boundary
        return headers, ok

    # ── manifest steps ───────────────────────────────────────────────────────
    def upload_file(self, client, ctx, file_path: str, file_name: str) -> str | None:
        url = self._url(ctx, "upload")
        headers, ok = self._headers(ctx, _ab.auth_path("file-upload"), json_ct=False)
        if not ok:
            return None
        with open(file_path, "rb") as fh:
            r = client.post(url, headers=headers,
                            files={"file": (file_name, fh, "application/octet-stream")},
                            data={"file_name": file_name, "permission": "PRIVATE"})
        if r.status_code >= 400:
            log.warning("AN3 file-upload HTTP %s: %s", r.status_code, r.text[:200])
            return None
        j = r.json() if r.content else {}
        return j.get("file_id") or (j.get("data") or {}).get("file_id") or j.get("__auto_id__")

    def create_dataset(self, client, ctx, name: str, dataset_type: str, file_id: str) -> str | None:
        url = self._url(ctx, "create")
        headers, ok = self._headers(ctx, _ab.auth_path("create-data-set"))
        if not ok:
            return None
        body = {"name": name, "dataset_type": dataset_type, "file_id": file_id}
        r = client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            log.warning("AN3 create-data-set HTTP %s: %s", r.status_code, r.text[:200])
            return None
        j = r.json() if r.content else {}
        return j.get("dataset_id") or (j.get("data") or {}).get("dataset_id") or j.get("__auto_id__")

    def poll_status(self, client, ctx, dataset_id: str, *, timeout: float = _INGEST_TIMEOUT) -> bool:
        url = self._url(ctx, "status")
        headers, ok = self._headers(ctx, _ab.auth_path("dataset-status"))
        if not ok:
            return False
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(url, headers=headers, params={"dataset_id": dataset_id})
            st = ""
            if r.status_code < 400 and r.content:
                j = r.json()
                st = str(j.get("status") or (j.get("data") or {}).get("status") or "").lower()
            if st in ("ready", "completed", "active", "success", "ingested"):
                return True
            if st in ("failed", "error"):
                return False
            time.sleep(float(os.environ.get("ARTA_AN_STATUS_POLL_S", "3")))
        return False

    def delete_dataset(self, client, ctx, dataset_id: str) -> None:
        url = self._url(ctx, "delete")
        headers, _ = self._headers(ctx, _ab.auth_path("delete-data-set"))
        try:
            client.request("DELETE", url, headers=headers, params={"dataset_id": dataset_id})
        except Exception as exc:
            log.warning("AN3 delete-data-set failed for %s: %s", dataset_id, exc)

    # ── A8: the APP layer (the analytics query is app-scoped) ─────────────────
    def list_apps(self, client, ctx) -> list[dict]:
        """A8.2 — GET the SUT's apps (read-only). [] on any failure.

        LIVE-GROUNDED (2026-06-27): the live SUT response is `{status, apps:[{app_id,
        app_name, app_description, …}]}` — the list is under `apps`, NOT `data`
        (reading `.data` is why app resolution looked empty + the query 422'd on a
        null app_id). Prefer `apps`, fall back to `data`/top-level list per SUT."""
        url = self._url(ctx, "list_apps")
        headers, ok = self._headers(ctx, _ab.auth_path("get-list-of-apps"))
        if not ok:
            return []
        try:
            r = client.get(url, headers=headers)
            if r.status_code >= 400 or not r.content:
                log.info("A8 get-list-of-apps HTTP %s: %s", r.status_code, r.text[:160])
                return []
            j = r.json()
            if isinstance(j, list):
                return j
            data = j.get("apps") or j.get("data") if isinstance(j, dict) else None
            return data if isinstance(data, list) else []
        except Exception as exc:
            log.info("A8 get-list-of-apps failed: %s", exc)
            return []

    def create_app(self, client, ctx, name: str, dataset_ids: list | None = None) -> str | None:
        """A8.2 — POST create-app (a WRITE — R154-gated). LIVE-GROUNDED shape: the SUT
        requires `{app_name, app_description, dataset_ids:[...]}` — an app BUNDLES
        datasets (live-confirmed 200; without dataset_ids → 422). Returns the app_id.

        ★ RESOLVED GAP (2026-07-23, SUT-source + mongo-verified): create-app returns only
        {status,message} (NO id), but the SUT computes app_id DETERMINISTICALLY as
        `md5(user_id + app_name)` (user_managment_router.create_app:842; app_master.app_id
        == md5(user_id+app_name) MATCH=True). So ARTA COMPUTES the app_id itself rather than
        depending on the (empty) response — closing the old 'app_id UNRESOLVED' gap."""
        url = self._url(ctx, "create_app")
        headers, ok = self._headers(ctx, _ab.auth_path("create-app"))
        if not ok:
            return None
        # SUT model = CreateAppData{app_name, app_description, dataset_ids} (send both the
        # legacy `name`/`description` keys AND the model keys for tolerance).
        body = {"name": name, "app_name": name,
                "description": f"ARTA test app {name}", "app_description": f"ARTA test app {name}",
                "dataset_ids": list(dataset_ids or [])}
        try:
            r = client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                log.warning("A8 create-app HTTP %s: %s", r.status_code, r.text[:200])
                return None
            j = r.json() if r.content else {}
            d = j.get("data") if isinstance(j.get("data"), dict) else j
            aid = _app_id_of(d) or _app_id_of(j)
            if not aid:
                # SUT returns no id → compute the deterministic app_id it used.
                aid = _deterministic_app_id(ctx, name)
                log.info("A8 create-app: computed deterministic app_id=%s (md5(user_id+app_name))",
                         str(aid)[:8])
            return aid
        except Exception as exc:
            log.warning("A8 create-app failed: %s", exc)
            return None

    def delete_app(self, client, ctx, app_id: str) -> None:
        url = self._url(ctx, "delete_app")
        headers, _ = self._headers(ctx, _ab.auth_path("delete-app"))
        try:
            client.request("DELETE", url, headers=headers, params={"app_id": app_id})
        except Exception as exc:
            log.warning("A8 delete-app failed for %s: %s", app_id, exc)

    @contextlib.contextmanager
    def seeded_dataset(self, file_path: str, *, dataset_type: str = "structured",
                       base_name: str = "rec", mode: str = None):
        """Seed a namespaced dataset from `file_path`, yield its dataset_id (or None
        on failure), and GUARANTEE teardown (delete in `finally`). Raises only on
        the R154 gate. On any seed step failure, yields None so the caller emits a
        truthful BLOCKED (`ingestion_failed`) rather than a false analytics-FAIL.
        `mode` is accepted for signature-parity with the v2 (manifest-driven) seeder;
        the v1 legacy path is single-mode so it is ignored here."""
        import httpx
        allowed, reason = seed_required()
        if not allowed:
            raise PermissionError(f"AN3 R154 gate: SUT data-seeding not permitted — {reason}")
        ctx = self._context()
        name = f"{_namespace()}__{base_name}__{uuid.uuid4().hex[:8]}"
        dataset_id = None
        with httpx.Client(timeout=_INGEST_TIMEOUT, verify=False) as client:
            try:
                # AL.1 — resolve the REAL fixture file + upload with its REAL
                # extension. The generator writes .csv when pandas is absent (the
                # arta-api container has none) while the embedded path may say
                # .parquet → use the sibling that actually exists. A mislabeled name
                # would be mis-parsed; the accepted format is a SUT contract.
                from pathlib import Path as _P
                _fp = _P(file_path)
                if not _fp.exists():
                    for _alt in (_fp.with_suffix(".csv"), _fp.with_suffix(".parquet"),
                                 _fp.with_suffix(".json")):
                        if _alt.exists():
                            file_path, _fp = str(_alt), _alt
                            break
                _ext = _fp.suffix or ".csv"
                file_id = self.upload_file(client, ctx, file_path, name + _ext)
                if file_id:
                    dataset_id = self.create_dataset(client, ctx, name, dataset_type, file_id)
                if dataset_id and self.poll_status(client, ctx, dataset_id):
                    log.info("AN3: seeded dataset %s (ns=%s) ready", dataset_id, _namespace())
                    # A8.3/A8.5 — the query is APP-SCOPED and needs the dataset NAME.
                    # Resolve/create the app (R154 on here → create allowed) and pin
                    # the name so the generated correctness query carries both → no 422.
                    os.environ["ARTA_ANALYTICS_DATASET_NAME"] = name
                    # live-grounded: the app bundles THIS dataset (create-app needs dataset_ids)
                    resolve_app_id(ctx, allow_create=True, dataset_ids=[dataset_id])
                    yield dataset_id
                else:
                    log.warning("AN3: dataset seed/ingest failed (file_id=%s dataset_id=%s)",
                                bool(file_id), dataset_id)
                    yield None
            finally:
                os.environ.pop("ARTA_ANALYTICS_DATASET_NAME", None)
                if dataset_id:
                    self.delete_dataset(client, ctx, dataset_id)  # GUARANTEED teardown
                    log.info("AN3: torn down dataset %s", dataset_id)


dataset_client = DatasetClient()


# ── A8: app_id resolution helpers (module-level; the query is app-scoped) ──────
def _app_id_of(rec) -> str | None:
    if not isinstance(rec, dict):
        return None
    for k in ("app_id", "analytics_app_id", "id", "__auto_id__"):
        v = rec.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _select_app(apps: list[dict]) -> str | None:
    """A8.2 — pick an app_id from the SUT's apps. When a sandbox namespace is set,
    prefer an app whose name carries it; else take the first listed app."""
    ns = os.environ.get("SUT_TEST_DATA_NAMESPACE", "").strip()
    if ns:
        for a in apps:
            nm = str(a.get("app_name") or a.get("name") or "")
            if ns in nm:
                return _app_id_of(a)
    return next((_app_id_of(a) for a in apps if _app_id_of(a)), None)


def _project_user_id(ctx: dict) -> str:
    """The SUT's project_user_id = md5(subscriber+subscription+workspace+project) — the key
    for user_master AND the app_id computation (get_user_id, files-consumer + analytics-agent;
    live-verified == cb1dbe66 for our tenant). workspace_id/project_id come from the agent
    token claims (the session token lacks them)."""
    import hashlib
    sub = ctx.get("subscriber_id") or ""
    subn = ctx.get("subscription_id") or ""
    from . import ingestion as _ai   # lazy — avoid the ingestion↔dataset import cycle
    cl = _ai._agent_claims() or {}
    ws = cl.get("workspace_id") or ""
    proj = _ai._project_id(ctx) or cl.get("project_id") or ""
    return hashlib.md5(f"{sub}{subn}{ws}{proj}".encode("utf-8")).hexdigest()


def _deterministic_app_id(ctx: dict, app_name: str) -> str:
    """The SUT computes app_id = md5(user_id + app_name) (create_app:842; mongo-verified
    MATCH). Lets ARTA know the app_id of an app it created even though create-app returns
    only {status,message}."""
    import hashlib
    return hashlib.md5(f"{_project_user_id(ctx)}{app_name}".encode("utf-8")).hexdigest()


def resolve_app_id(ctx: dict | None = None, *, allow_create: bool | None = None,
                   dataset_ids: list | None = None) -> str | None:
    """A8.2 KEYSTONE — resolve a VALID analytics app_id (the query is app-scoped;
    a null app_id is the 422 root cause). Precedence, READ-ONLY first (R154-safe):
      1. ARTA_ANALYTICS_APP_ID env (operator/prior resolution — memoized here)
      2. storage localStorage app_id (the SPA's selected app)
      3. GET get-list-of-apps → select an existing app (namespace-matched)
      4. POST create-app — ONLY under the R154 opt-in (a write)
    Memoizes the result into ARTA_ANALYTICS_APP_ID so the live GET runs once. Returns
    None (→ caller fails truthfully, A8.4) when no app can be resolved. Fast-None when
    the context has no base_url/tokens (unit-safe — never hits the network blindly).

    ★ A8.5 (2026-07-23) — CORRECTNESS seeds pass `dataset_ids`: the analytics query is
    app-scoped, so it must run against an app that BUNDLES the seeded dataset. Reusing an
    arbitrary existing app (env/storage/list) points the query at a DIFFERENT app that does
    NOT contain our dataset → the SUT answers "no files". So when `dataset_ids` are given +
    we may write, CREATE A FRESH APP bundling them (its app_id is deterministic,
    md5(user_id+app_name)) and use THAT. Killswitch ARTA_AN_APP_PER_DATASET_DISABLE=1."""
    dc = DatasetClient()
    ctx = ctx or dc._context()
    _allow = (seed_required()[0] if allow_create is None else allow_create)
    if (dataset_ids and _allow and os.environ.get("ARTA_AN_APP_PER_DATASET_DISABLE") != "1"
            and ctx.get("base_url") and (ctx.get("tokens") or {}).get("agent_api_token")):
        import httpx
        try:
            with httpx.Client(timeout=30, verify=False) as client:
                aname = f"{_namespace()}__app__{uuid.uuid4().hex[:8]}"
                aid = dc.create_app(client, ctx, aname, dataset_ids=list(dataset_ids))
                if aid:
                    os.environ["ARTA_ANALYTICS_APP_ID"] = aid
                    os.environ["ARTA_ANALYTICS_APP_NAME"] = aname
                    log.info("A8.5: created fresh app %s bundling dataset(s) %s (query-scoped)",
                             str(aid)[:8], dataset_ids)
                    return aid
        except Exception as exc:
            log.info("A8.5: per-dataset app create failed (falling back): %s", exc)
        # fall through to the legacy precedence when the fresh-app path can't create one
    env_app = os.environ.get("ARTA_ANALYTICS_APP_ID")
    if env_app:
        return env_app
    storage_app = (ctx.get("dataset") or {}).get("app_id")
    if storage_app:
        os.environ["ARTA_ANALYTICS_APP_ID"] = storage_app
        return storage_app
    if os.environ.get("ARTA_ANALYTICS_APP_AUTORESOLVE") == "0":
        return None
    if not (ctx.get("base_url") and ctx.get("subscriber_id") and ctx.get("subscription_id")
            and (ctx.get("tokens") or {}).get("agent_api_token")):
        return None  # no live context → don't attempt the network (unit-safe)
    import httpx
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            chosen = _select_app(dc.list_apps(client, ctx))  # read-only
            if not chosen:
                allow = seed_required()[0] if allow_create is None else allow_create
                if allow:
                    # live-grounded: an app BUNDLES datasets → pass dataset_ids
                    chosen = dc.create_app(
                        client, ctx, f"{_namespace()}__app__{uuid.uuid4().hex[:8]}",
                        dataset_ids=dataset_ids)
            if chosen:
                os.environ["ARTA_ANALYTICS_APP_ID"] = chosen
                log.info("A8: resolved analytics app_id=%s", str(chosen)[:8])
                return chosen
    except Exception as exc:
        log.info("A8: app_id resolution failed: %s", exc)
    return None
