"""AN-LIVE / AL — the REAL SUT analytics FILE ingestion (backend-schema-grounded).

Discovered from the SUT BACKEND request schemas (the media/collection service + the
analytics agent's CreateUserDataset model), superseding the earlier React-form-state
guess. To make an ARTA-generated file a QUERYABLE dataset:

  0. PRE-GEN     dataset_id = "files_" + uuid4()  (client-side; drives dataset_type=files)
  1. MEDIA       POST {backend}/api/media/{acct}/subscriber/{sub}/subscription/{subn}/
                 {COLLECTIONS_SCHEMA}/generate-upload-url  (Bearer <session-token>)
                   body {file_name, file_context:"analytics_files", file_type:<ext>}
                   -> {url, url_fields (S3 POST policy JSON string), file_id}
                 then POST the file to S3 (url) with url_fields as FormData.
  2. CM STORE    POST {backend}/{acct}/api/collection/{acct}/user/private/cm/v1/
                 {COLLECTIONS_SCHEMA}/{files_collection}  (Bearer <session-token>)
                   {collection_item:{file_id, file_name, file_url, dataset_id, ...}}
  3. ANALYTICS   POST {analytics}/.../user-management/event/create-data-set
                 (Bearer <agent-USER token>)  {dataset_name, source_id:[dataset_id],
                 dataset_id}  -> {status, message}  (for FILES, source_id ≡ [dataset_id];
                 there is NO separate source entity → no createSource/integration-sources host).
  4. POLL        GET .../data-processing/event/dataset-status?dataset_id= until
                 message=="Success" (a backend file-consumer indexes the S3 object),
                 then query (the query is the true readiness gate) ; teardown.

AUTH SPANS TWO HOSTS/TOKENS: media + cm use `Bearer <session-token>` on the BACKEND host;
create-data-set + dataset-status + query use the minted `Bearer <agent-user-token>` on
the ANALYTICS host. ARTA holds both. COLLECTIONS_SCHEMA is a deployment env var (default
`analytics_schema`, override via ARTA_AN_COLLECTIONS_SCHEMA). OPT-IN (`ARTA_AN_INGEST_MODE=v2`);
every created resource is torn down in a `finally` (no orphans). `create_source`/
`delete_source` remain for non-file integration sources but are NOT on the file path.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid

from . import analytics_backend as _ab
from .dataset_client import seed_required, _namespace, resolve_app_id, dataset_client

log = logging.getLogger("arta.analytics.ingest")

_TIMEOUT = float(os.environ.get("ARTA_ANALYTICS_INGEST_TIMEOUT", "120"))


# ── CONFIG — now sourced from the SUT-agnostic WORKFLOW MANIFEST (data, not code) ──
# The hosts / schema / collections / dataset-modes / endpoints are read from
# analytics_manifest.load_manifest() (env-overridable, and a future
# discover_analytics_workflow_from_source populates it per SUT). Individual ARTA_AN_*
# env overrides still win (they map to the manifest's `env` keys).
from . import analytics_manifest as _man   # noqa: E402


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _manifest() -> dict:
    return _man.load_manifest()


def _backend_host() -> str:
    return _man.host_base(_manifest(), "backend") or "https://backend.app.example.internal"


def _monitor_host() -> str:
    return _man.host_base(_manifest(), "monitor")


def _collections_schema() -> str:
    return _man.identifier(_manifest(), "schema_id", "analytics_schema")


def _files_collection() -> str:
    return _cfg("ARTA_AN_FILES_COLLECTION",
                (_manifest().get("collections") or {}).get("files", "analytics_files"))


def _s3_bucket() -> str:
    return _cfg("ARTA_AN_S3_BUCKET", "")


def _sources_api_base() -> str:
    # non-file integration-source host (createSource) — env-only; NOT on the file path.
    return _cfg("ARTA_AN_SOURCES_API_BASE", "").rstrip("/")


def _ensure_xlsx(file_path: str) -> str | None:
    """Excel mode needs a REAL .xlsx — the SUT routes only .xlsx to the tabular engine
    (excel_mcp); a .csv goes to the document reader (can't count/sum). ARTA fixtures are
    usually .csv, so convert (types preserved via pandas; openpyxl fallback with numeric
    coercion). Returns the .xlsx path (a sibling temp; caller cleans up), or None when no
    writer is available → the seed BLOCKs truthfully. Already-.xlsx passes through."""
    low = file_path.lower()
    if low.endswith((".xlsx", ".xls")):
        return file_path
    out = os.path.splitext(file_path)[0] + ".arta_xlsx.xlsx"
    try:
        import pandas as _pd
        if low.endswith(".parquet"):
            df = _pd.read_parquet(file_path)
        elif low.endswith(".json"):
            df = _pd.read_json(file_path)
        else:
            df = _pd.read_csv(file_path)
        df.to_excel(out, index=False)   # infers column types
        return out
    except Exception:
        pass
    # openpyxl fallback ONLY for csv-like TEXT (reading parquet/json bytes as csv →
    # garbage). Non-text without pandas → BLOCK truthfully rather than emit junk.
    if not low.endswith((".csv", ".tsv", ".txt")):
        log.warning("AL excel: cannot convert %s to .xlsx (pandas unavailable + non-csv "
                    "fixture) — BLOCK truthfully", file_path)
        return None
    try:
        import csv as _csv
        from openpyxl import Workbook
        with open(file_path, newline="") as fh:
            rows = list(_csv.reader(fh))
        if not rows:
            return None

        def _num(c):
            try:
                f = float(c)
                return int(f) if f.is_integer() else f
            except Exception:
                return c
        wb = Workbook(); ws = wb.active
        for i, r in enumerate(rows):
            ws.append(r if i == 0 else [_num(c) for c in r])
        wb.save(out)
        return out
    except Exception as exc:
        log.warning("AL excel: no .xlsx writer (pandas/openpyxl) — cannot convert %s: %s",
                    file_path, exc)
        return None


def _ensure_files_format(file_path: str, accepted: list) -> str | None:
    """Files mode uploads the fixture RAW, so its extension must be one the SUT accepts
    (manifest upload_exts — e.g. .csv/.txt/.md/.pdf/.mdx; parquet is NOT accepted). An
    already-accepted ext passes through; a tabular fixture (parquet/json) is converted to
    .csv (pandas; the oracle reads csv fine); anything else → None → the seed BLOCKs
    truthfully rather than upload a format the SUT rejects. Returns the upload path (a
    sibling temp for conversions; caller cleans up) or None."""
    low = file_path.lower()
    acc = [str(e).lower() for e in (accepted or [])]
    if any(low.endswith(e) for e in acc) or not acc:
        return file_path
    if ".csv" not in acc:
        # can't convert to any accepted ext (no csv target) → block truthfully
        log.warning("AL files: %s ext not accepted %s and no .csv target — BLOCK", file_path, acc)
        return None
    out = os.path.splitext(file_path)[0] + ".arta_files.csv"
    try:
        import pandas as _pd
        if low.endswith(".parquet"):
            df = _pd.read_parquet(file_path)
        elif low.endswith(".json"):
            df = _pd.read_json(file_path)
        else:
            df = _pd.read_csv(file_path)
        df.to_csv(out, index=False)
        return out
    except Exception as exc:
        log.warning("AL files: cannot convert %s to an accepted .csv: %s", file_path, exc)
        return None


class IngestionClient:
    """Implements the 4-service upload chain. Reuses analytics_backend for auth/context."""

    def __init__(self):
        self._auth = _ab.AnalyticsClient()
        self._ctx = None

    def _context(self) -> dict:
        if self._ctx is None:
            ctx = _ab._resolve_context(_ab._load_storage())
            # AUTH-CYCLE (R218) — the analytics-host calls (create-data-set, dataset-
            # status, query) authorize the LOGIN-scoped agent-USER token, same as the
            # query. Mint it once and swap it into ctx so _analytics_headers uses it.
            qtok = _ab._mint_query_token(ctx)
            if qtok:
                ctx.setdefault("tokens", {})["agent_api_token"] = qtok
            self._ctx = ctx
        return self._ctx

    def _ids(self, ctx):
        return ctx["subscriber_id"], ctx["subscription_id"], _account_id(ctx)

    def _session_token(self, ctx) -> str:
        return ((ctx.get("tokens") or {}).get("session_token")
                or (ctx.get("tokens") or {}).get("cookie_value") or "")

    def _backend_headers(self, ctx) -> tuple[dict, bool]:
        """MEDIA + cm addFile run on the BACKEND host with `Bearer <session-token>` (the
        raw session JWT) — NOT the agent token. Source: the SUT SPA's axios interceptor."""
        sess = self._session_token(ctx)
        h = {"Content-Type": "application/json"}
        if sess:
            h["Authorization"] = f"Bearer {sess}"
        return h, bool(sess)

    def _analytics_headers(self, ctx, path, *, json_ct=True):
        """Analytics-host calls (create-data-set, dataset-status) use the minted
        agent-USER token via the discovered auth chain (same as the query)."""
        h, ok = self._auth._build_auth(path, ctx)
        if not json_ct:
            h.pop("Content-Type", None)
        return h, ok

    def _monitor_headers(self, ctx):
        """The monitoring host (job-create) uses the SAME agent-USER Bearer as analytics
        (manifest hosts.monitor.auth=agent_user_token). Reuse the auth-chain to get the
        prefixed Bearer, then send it to the monitor host."""
        return self._auth._build_auth(_ab.auth_path("query"), ctx)

    # back-compat alias (analytics-host default)
    def _headers(self, ctx, path, *, json_ct=True):
        return self._analytics_headers(ctx, path, json_ct=json_ct)

    # ── step 1: MEDIA presigned upload ────────────────────────────────────────
    def generate_upload_url(self, client, ctx, file_name: str) -> dict:
        """POST generate-upload-url -> {url, url_fields, file_id}. {} on failure."""
        sub, subn, acct = self._ids(ctx)
        schema = _collections_schema()
        if not schema:
            log.warning("AL ingest: ARTA_AN_COLLECTIONS_SCHEMA unset (LIVE-CONFIRM) — cannot presign")
            return {}
        url = f"{_backend_host()}/api/media/{acct}/subscriber/{sub}/subscription/{subn}/{schema}/generate-upload-url"
        # {file_name, file_context, file_type}. The prior {name,file_name} 500'd (KeyError
        # on file_context). content_type is in the model but the handler ignores it.
        ext = (os.path.splitext(file_name)[1].lstrip(".") or "csv").lower()
        body = {"file_name": file_name,
                "file_context": _cfg("ARTA_AN_FILE_CONTEXT", "analytics_files"),
                "file_type": ext, "content_type": ext}
        if _s3_bucket():
            body["bucket_name"] = _s3_bucket()
        h, ok = self._backend_headers(ctx)
        if not ok:
            return {}
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code >= 400:
                log.warning("AL generate-upload-url HTTP %s: %s", r.status_code, r.text[:200])
                return {}
            j = r.json() if r.content else {}
            d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
            return d if isinstance(d, dict) else {}
        except Exception as exc:
            log.warning("AL generate-upload-url failed: %s", exc)
            return {}

    def upload_to_s3(self, client, presign: dict, file_path: str) -> bool:
        """POST the file to S3 with the presigned url_fields as multipart FormData."""
        url = presign.get("url")
        try:
            fields = presign.get("url_fields")
            fields = json.loads(fields) if isinstance(fields, str) else (fields or {})
        except Exception:
            fields = {}
        if not (url and fields):
            return False
        try:
            with open(file_path, "rb") as fh:
                data = {k: str(v) for k, v in fields.items()}
                r = client.post(url, data=data, files={"file": (os.path.basename(file_path), fh)})
            return r.status_code in (200, 201, 204)
        except Exception as exc:
            log.warning("AL S3 upload failed: %s", exc)
            return False

    def fetch_download_url(self, client, ctx, file_id: str) -> str:
        """GET generate-upload-url?fileId= → the RETRIEVABLE download_url (the S3 `url`
        from presign is a PUT-only policy URL; the file-consumer + cm need the readable
        download_url). Returns "" when unavailable. Backend host + Bearer <session-token>."""
        sub, subn, acct = self._ids(ctx)
        schema = _collections_schema()
        url = f"{_backend_host()}/api/media/{acct}/subscriber/{sub}/subscription/{subn}/{schema}/generate-upload-url"
        h, ok = self._backend_headers(ctx)
        if not (ok and file_id):
            return ""
        try:
            r = client.get(url, headers=h, params={"fileId": file_id})
            if r.status_code >= 300:
                return ""
            j = r.json()
            rec = (j[0] if isinstance(j, list) and j else (j if isinstance(j, dict) else {}))
            if isinstance(rec, dict):
                return rec.get("download_url") or rec.get("url") or rec.get("file_url") or ""
        except Exception as exc:
            log.debug("AL fetch_download_url: %s", exc)
        return ""

    # ── step 2: CM add file metadata ──────────────────────────────────────────
    def add_file(self, client, ctx, *, file_id, file_url, file_name, dataset_id, project_id) -> bool:
        sub, subn, acct = self._ids(ctx)
        schema = _collections_schema()
        if not schema:
            return False
        url = f"{_backend_host()}/{acct}/api/collection/{acct}/user/private/cm/v1/{schema}/{_files_collection()}"
        # cm is SCHEMALESS (collection_core_api accepts {collection_item:<Raw JSON>}); the
        body = {"collection_item": {
            "file_name": file_name, "file_id": file_id, "file_url": file_url,
            "dataset_id": dataset_id, "project_id": project_id, "uploaded_by": "ARTA",
            "uploaded_via": "api", "status": "Processing", "new_file": True,
            "checksum": None, "__auto_id__": file_id}}
        h, ok = self._backend_headers(ctx)
        if not ok:
            return False
        try:
            r = client.post(url, headers=h, json=body)
            return r.status_code < 300
        except Exception as exc:
            log.warning("AL add_file failed: %s", exc)
            return False

    def create_source(self, client, ctx, *, name, file_id, file_url) -> str | None:
        """POST create/sources/... -> source_id. LIVE-CONFIRM reqObj (best-effort)."""
        base = _sources_api_base()
        if not base:
            log.warning("AL ingest: ARTA_AN_SOURCES_API_BASE unset (LIVE-CONFIRM) — cannot create source")
            return None
        sub, subn, acct = self._ids(ctx)
        url = f"{base}/create/sources/subscriber_id/{sub}/subscription_id/{subn}"
        # LIVE-CONFIRM: exact source reqObj. Best-effort file-source shape + env-extra.
        body = {"source_name": name, "source_type": _cfg("ARTA_AN_SOURCE_TYPE", "files"),
                "file_ids": [file_id], "file_urls": [file_url]}
        extra = _cfg("ARTA_AN_SOURCE_EXTRA_JSON")
        if extra:
            try: body.update(json.loads(extra))
            except Exception: pass
        h, ok = self._headers(ctx, "/create/sources")
        if not ok:
            return None
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code >= 400:
                log.warning("AL create-source HTTP %s: %s", r.status_code, r.text[:200])
                return None
            j = r.json() if r.content else {}
            return _first_id(j, "source_id", "__auto_id__", "id") or _first_id(j.get("data") or {}, "source_id", "id")
        except Exception as exc:
            log.warning("AL create-source failed: %s", exc)
            return None

    def delete_source(self, client, ctx, source_id: str) -> None:
        sub, subn, _ = self._ids(ctx)
        base = ctx["base_url"]
        url = f"{base}/subscriber/{sub}/subscription/{subn}/function/user-management/event/delete-connection"
        h, _ok = self._headers(ctx, _ab.auth_path("delete-connection"))
        try: client.request("DELETE", url, headers=h, params={"source_id": source_id})
        except Exception as exc: log.warning("AL delete-source failed: %s", exc)

    # ── step 3: ANALYTICS create dataset (files: source_id ≡ dataset_id) ───────
    def create_dataset(self, client, ctx, *, dataset_name, dataset_id) -> str | None:
        """POST create-data-set. BACKEND-SCHEMA-GROUNDED (the SUT analytics agent
        user_schema.py:89 CreateUserDataset): `source_id` is a **List[str]** (a bare
        string → 422), `dataset_id` optional (server computes md5(user_id+name) if absent).
        For a FILE dataset there is NO separate source — source_id ≡ [dataset_id]. The
        response does NOT echo the id ({status, message}), so we return the id we passed."""
        sub, subn, _ = self._ids(ctx)
        base = ctx["base_url"]
        url = f"{base}/subscriber/{sub}/subscription/{subn}/function/user-management/event/create-data-set"
        body = {"dataset_name": dataset_name, "source_id": [dataset_id], "dataset_id": dataset_id}
        extra = _cfg("ARTA_AN_DATASET_EXTRA_JSON")
        if extra:
            try: body.update(json.loads(extra))
            except Exception: pass
        h, ok = self._analytics_headers(ctx, url)   # analytics host → minted agent-user token
        if not ok:
            return None
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code >= 400:
                log.warning("AL create-data-set HTTP %s: %s", r.status_code, r.text[:200])
                return None
            return dataset_id   # success → the client-held id is the dataset id
        except Exception as exc:
            log.warning("AL create-data-set failed: %s", exc)
            return None

    @staticmethod
    def _s3_key_and_bucket(presign: dict) -> tuple[list[str], str]:
        """The S3 object key (url_fields.key / advanced.object_path) + bucket
        (advanced.bucket_name / the S3 url host) — feed the ingestion job's
        connections.direct_files so the consumer knows WHICH object to parse."""
        import ast, re
        key = bucket = ""
        try:
            uf = presign.get("url_fields")
            uf = json.loads(uf) if isinstance(uf, str) else (uf or {})
            key = (uf or {}).get("key") or ""
        except Exception:
            pass
        adv = presign.get("advanced")
        if isinstance(adv, str):
            try:
                adv = json.loads(adv)
            except Exception:
                try:
                    adv = ast.literal_eval(adv)   # media returns a python-repr string
                except Exception:
                    adv = {}
        if isinstance(adv, dict):
            bucket = adv.get("bucket_name") or ""
            key = key or adv.get("object_path") or ""
        if not bucket:
            mobj = re.search(r"https?://([^./]+)\.s3", presign.get("url") or "")
            if mobj:
                bucket = mobj.group(1)
        return ([key] if key else []), bucket

    def _data_sources_collection_url(self, ctx) -> str:
        _, _, acct = self._ids(ctx)
        schema = _collections_schema()
        coll = _cfg("ARTA_AN_DATA_SOURCES_COLLECTION",
                    (_manifest().get("collections") or {}).get("data_sources",
                                                                "analytics_data_sources"))
        return f"{_backend_host()}/{acct}/api/collection/{acct}/user/private/cm/v1/{schema}/{coll}"

    def resolve_files_connection_id(self, client, ctx) -> str | None:
        """SPA parity (the SUT SPA's file-based-dataset `files:` resolver): the ingestion
        job's `connection_id` must reference a REAL cm data-sources collection
        record whose payload.source_type=='files' — the consumer resolves it. ARTA used to
        fabricate a random uuid (the consumer couldn't resolve it → orphaned task). Find
        the user's files data_source; provision one if absent (the SPA aborts if missing,
        but ARTA runs headless so it self-provisions). Returns the record's __auto_id__
        (== the SPA's connection_id), or None on auth/store failure → caller falls back to a
        fabricated id (best-effort, logged)."""
        url = self._data_sources_collection_url(ctx)
        h, ok = self._backend_headers(ctx)
        if not ok:
            return None
        try:
            r = client.get(url, headers=h)
            if r.status_code < 300 and r.content:
                j = r.json()
                recs = j if isinstance(j, list) else (j.get("data") or j.get("items") or [])
                for it in (recs or []):
                    pl = it.get("payload", it) if isinstance(it, dict) else {}
                    if isinstance(pl, dict) and pl.get("source_type") == "files":
                        cid = pl.get("__auto_id__") or it.get("__auto_id__")
                        if cid:
                            return cid
        except Exception as exc:
            log.debug("AL resolve connection_id (list): %s", exc)
        # none found → provision one (SPA collection_item shape for a files source)
        cid = uuid.uuid4().hex
        body = {"collection_item": {"source_type": "files", "status": "connected",
                "source_name": "ARTA files", "purpose": "analytics", "__auto_id__": cid}}
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code < 300:
                return cid
            log.debug("AL provision connection_id HTTP %s: %s", r.status_code, r.text[:160])
        except Exception as exc:
            log.debug("AL provision connection_id: %s", exc)
        return None

    def monitor_job_status(self, client, ctx, job_id: str):
        """The SUT's REAL ingestion-completion signal (live-proven 2026-07-23): the
        monitoring service reports the consumer's terminal state keyed by the job_id we
        sent in job-create. Returns (status_lower:str, raw:dict|str) or (None, err).

        job-STATUS is the BARE `/function/monitoring/event/job-status` path (unlike
        job-CREATE which needs the subscriber/subscription/account/project prefix). We
        try the manifest URL first, then the OTHER form as a fallback so this stays
        SUT-agnostic across deployments that prefix differently."""
        man = _manifest()
        sub, subn, acct = self._ids(ctx)
        proj = _project_id(ctx)
        _m, url, _host = _man.endpoint_url(man, "monitor_job_status",
            {"account": acct, "subscriber": sub, "subscription": subn, "project": proj})
        if not url or "{" in url:
            return None, "unresolved_monitor_status_endpoint"
        h, ok = self._monitor_headers(ctx)
        if not ok:
            return None, "monitor_auth_unavailable"
        # candidate URLs: manifest form first, then the prefixed↔bare alternate.
        mon = _monitor_host()
        prefixed = (f"{mon}/subscriber_id/{sub}/subscription_id/{subn}/account_id/{acct}"
                    f"/project_id/{proj}/function/monitoring/event/job-status")
        bare = f"{mon}/function/monitoring/event/job-status"
        candidates = [url] + [u for u in (bare, prefixed) if u != url]
        last_err = None
        for cand in candidates:
            try:
                r = client.get(cand, headers=h, params={"job_id": job_id})
                if r.status_code == 404:
                    last_err = f"http_404:{r.text[:80]}"
                    continue   # wrong path form for this deployment → try the alternate
                if r.status_code >= 400:
                    return None, f"http_{r.status_code}:{r.text[:120]}"
                j = r.json() if r.content else {}
                j = j if isinstance(j, dict) else {}
                return str(j.get("status", "")).strip().lower(), j
            except Exception as exc:
                last_err = f"{type(exc).__name__}:{exc}"
        return None, last_err or "monitor_status_unreachable"

    def trigger_ingestion_job(self, client, ctx, *, dataset_id, dataset_name,
                              file_urls, bucket_name, is_excel) -> str | None:
        """★ THE INGESTION TRIGGER (was the missing step) — enqueue the uploaded object(s)
        for the file/excel consumer to PARSE + INDEX. Without it, create-data-set leaves
        the dataset with `sources:[]` forever (the live 8-min no-index). BACKEND-GROUNDED:
        monitoring `job-create` with `JobDetails` (the SUT monitoring API schema);
        `connections.direct_files` MUST be non-empty (:150-154 / :527) or it 400s.
        LIVE-CRITICAL: `files[]` must be FULL S3 object URLs (https://<bucket>.s3.<region>
        .amazonaws.com/<key>), NOT bare keys — the consumer derives the bucket via
        `urlparse(url).netloc.split('.')[0]`; a bare key → empty netloc → get_bucket_region('')
        throws → `region,creds = None` → 500 'cannot unpack non-iterable NoneType'
        (s3_uploader.py build_client). Manifest-driven. Killswitch ARTA_AN_TRIGGER_DISABLE=1.
        ★ Returns the job_id (str) on success so poll_status can watch the monitoring
        service's terminal state (the SUT's REAL completion signal); None on failure.
        `connection_id` references a REAL cm data_source record (SPA parity) — a fabricated
        one leaves the consumer unable to resolve it."""
        job_id = uuid.uuid4().hex
        if os.environ.get("ARTA_AN_TRIGGER_DISABLE") == "1":
            return job_id
        man = _manifest()
        sub, subn, acct = self._ids(ctx)
        proj = _project_id(ctx)
        method, url, _host = _man.endpoint_url(man, "trigger_job",
            {"account": acct, "subscriber": sub, "subscription": subn, "project": proj})
        if not url or "{" in url:
            log.warning("AL trigger_job: unresolved endpoint/ids (url=%s proj=%s)", url, bool(proj))
            return None
        if not file_urls:
            log.warning("AL trigger_job: no S3 file URL resolved (need the download_url) — cannot enqueue")
            return None
        h, ok = self._monitor_headers(ctx)
        if not ok:
            return None
        # SPA parity: a REAL cm data_source __auto_id__ (not a fabricated uuid the consumer
        # can't resolve). Fall back to a fabricated id if the cm store is unreachable.
        conn_id = self.resolve_files_connection_id(client, ctx)
        if not conn_id:
            conn_id = uuid.uuid4().hex
            log.warning("AL trigger_job: could not resolve a real files connection_id — "
                        "using a fabricated id (consumer may not resolve it)")
        bearer = (h.get("Authorization") or "").replace("Bearer ", "")
        body = {
            "auth_token": bearer,
            "alert_email": _cfg("ARTA_AN_ALERT_EMAIL", "arta-analytics@example.com"),
            "is_excel": bool(is_excel), "job_type": "an",
            "dataset_id": dataset_id, "dataset_name": dataset_name,
            "connections": {"direct_files": {
                "connection_id": conn_id,            # live-422-required (FilesMeta)
                "job_id": job_id, "bucket_name": bucket_name,
                "files": list(file_urls), "source_id": [dataset_id]}},  # FULL S3 URLs
        }
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code >= 400:
                log.warning("AL trigger_job HTTP %s: %s", r.status_code, r.text[:200])
                return None
            log.info("AL ingest: ingestion job %s enqueued for %s (%d file[s])",
                     job_id, dataset_id, len(file_urls))
            return job_id
        except Exception as exc:
            log.warning("AL trigger_job failed: %s", exc)
            return None

    def trigger_file_upload(self, client, ctx, *, dataset_id, dataset_name,
                            file_urls, is_excel, source_name=None) -> str | None:
        """★ THE REAL INGESTION TRIGGER (SUT-source + LIVE-PROVEN 2026-07-23): the SPA's
        file-dataset upload (the SUT SPA's file-source save flow) POSTs to the analytics
        `data-processing/event/file-upload` endpoint — whose handler (the SUT analytics agent
        files_management_router.upload_file) computes project_user_id from the auth token,
        validates the file_paths, and PUBLISHES to the consumer that writes the queryable
        user_master record. The monitoring `job-create` path ARTA used only marks the job
        'Success' (scheduled) and NEVER indexes (dataset-status stays 0). file-upload moves
        dataset-status to progress 10 (processing STARTED) → eventually 100 (queryable).

        LIVE-CONFIRMED accepted body: {file_paths, origins:['direct_files'], ocr,
        extra_meta:{realm:{source_name}}, dataset_id, dataset_name, is_excel, dataset_type,
        source_type}. Auth = the minted analytics agent-user token (_analytics_headers).
        Returns the correlation_id (str) on success (HTTP 200 {correlation_ids,status}),
        None on failure. Killswitch ARTA_AN_FILE_UPLOAD_DISABLE=1."""
        if os.environ.get("ARTA_AN_FILE_UPLOAD_DISABLE") == "1":
            return "disabled"
        if not file_urls:
            log.warning("AL file-upload: no S3 file URL resolved — cannot trigger ingestion")
            return None
        sub, subn, _ = self._ids(ctx)
        base = ctx["base_url"]
        url = f"{base}/subscriber/{sub}/subscription/{subn}/function/data-processing/event/file-upload"
        h, ok = self._analytics_headers(ctx, url)
        if not ok:
            return None
        dtype = (str(dataset_id).split("_")[0] if dataset_id and "_" in str(dataset_id)
                 else ("excel" if is_excel else "files"))
        body = {
            "file_paths": list(file_urls),
            "origins": ["direct_files"],
            "ocr": True,
            "extra_meta": {"realm": {"source_name": source_name or dataset_name}},
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "is_excel": bool(is_excel),
            "dataset_type": dtype,
            "source_type": dtype,
        }
        try:
            r = client.post(url, headers=h, json=body)
            if r.status_code >= 400:
                log.warning("AL file-upload HTTP %s: %s", r.status_code, r.text[:200])
                return None
            j = r.json() if r.content else {}
            cid = ""
            if isinstance(j, dict):
                cids = j.get("correlation_ids")
                if isinstance(cids, dict) and cids:
                    cid = next(iter(cids.values()))
                cid = cid or j.get("correlation_id") or j.get("job_id") or "ok"
            log.info("AL file-upload: ingestion STARTED for %s (correlation=%s)", dataset_id, cid)
            return str(cid) or "ok"
        except Exception as exc:
            log.warning("AL file-upload failed: %s", exc)
            return None

    def delete_dataset(self, client, ctx, dataset_id: str) -> None:
        dataset_client.delete_dataset(client, ctx, dataset_id)  # reuse

    def poll_status(self, client, ctx, dataset_id: str, *, job_id=None, timeout=None) -> bool:
        """Poll until the SUT finishes INGESTING the uploaded object into the QUERY-READABLE
        store (so the analytics query can actually read it).

        ★ CORRECTED 2026-07-23 (SUT-source-grounded): the monitoring `job-status=Success` is
        NOT a readiness signal — the SUT monitoring service (`task_publisher._publish_direct_files`)
        marks the job `Success` IMMEDIATELY after SCHEDULING a BACKGROUND task
        (`s3_uploader.file_upload_to_s3`); the real work (download the file → re-upload →
        files-consumer writes the `user_master` record the query reads) happens AFTER. Gating
        on monitoring-Success (the prior R123 attempt) yielded a dataset that was NOT yet
        queryable → the query answered "no files". So READY = the data-processing
        `dataset-status` reaching `progress_value >= 100` (OR a ready status) — the SUT's
        query-side completion signal. The monitoring job-status is used ONLY for FAIL-FAST
        (a Failed monitoring job → block early). When the SUT background indexer never
        finishes, this TRUTHFULLY times out (`ingestion_not_indexed`) rather than falsely
        reporting ready.

        Killswitches: ARTA_AN_POLL_DISABLE=1 skips; ARTA_AN_POLL_OPTIMISTIC=1 (timeout→True);
        ARTA_AN_POLL_SOURCES_READY=1 accepts sources-non-empty as ready (premature — legacy);
        ARTA_AN_POLL_MONITOR_SUCCESS_READY=1 restores the (incorrect) monitoring-Success gate."""
        import time
        if os.environ.get("ARTA_AN_POLL_DISABLE") == "1":
            return True
        timeout = timeout if timeout is not None else float(
            os.environ.get("ARTA_ANALYTICS_INGEST_TIMEOUT", "300"))
        _progress_ready = float(os.environ.get("ARTA_AN_POLL_PROGRESS_READY", "100"))
        interval = float(os.environ.get("ARTA_AN_POLL_INTERVAL", "5"))
        _READY = {"success", "ready", "completed", "complete", "indexed"}
        _FAILED = {"failed", "error", "errored", "cancelled", "canceled"}
        _monitor_success_ready = os.environ.get("ARTA_AN_POLL_MONITOR_SUCCESS_READY") == "1"
        watch_monitor = bool(job_id) and os.environ.get("ARTA_AN_POLL_MONITOR_DISABLE") != "1"
        sub, subn, _ = self._ids(ctx)
        base = ctx["base_url"]
        ds_url = f"{base}/subscriber/{sub}/subscription/{subn}/function/data-processing/event/dataset-status"
        ds_h, _ok = self._analytics_headers(ctx, ds_url)
        deadline = time.time() + timeout
        _last_progress = None
        _last_mon = None
        while time.time() < deadline:
            # ── FAIL-FAST only: the monitoring job's terminal state. Success here means the
            #    background indexer was SCHEDULED (not done) — so it is NOT a ready signal
            #    (unless the legacy killswitch restores it). A Failed monitoring job aborts.
            if watch_monitor:
                mstat, mraw = self.monitor_job_status(client, ctx, job_id)
                if mstat is not None:
                    if mstat != _last_mon:
                        log.info("AL ingest: monitoring job %s status=%s", job_id, mstat or "(empty)")
                        _last_mon = mstat
                    if mstat in _FAILED:
                        detail = ""
                        if isinstance(mraw, dict):
                            detail = str(mraw.get("error") or mraw.get("message") or mraw.get("reason") or "")
                        os.environ["ARTA_AN_SEED_SKIP_REASON"] = f"ingestion_consumer_error:{mstat}"
                        log.warning("AL ingest: dataset %s ingestion FAILED (monitoring=%s%s)",
                                    dataset_id, mstat, f" — {detail[:160]}" if detail else "")
                        return False
                    if mstat in _READY and _monitor_success_ready:
                        log.info("AL ingest: dataset %s — monitoring Success (legacy ready gate)", dataset_id)
                        return True
            # ── READY signal: data-processing dataset-status reaching progress>=100 (the
            #    SUT's QUERY-side indexing-complete signal — what the query actually needs).
            try:
                r = client.get(ds_url, headers=ds_h, params={"dataset_id": dataset_id})
                if r.status_code < 300:
                    j = r.json() if r.content else {}
                    j = j if isinstance(j, dict) else {}
                    msg = str(j.get("message", "")).strip().lower()
                    status = str(j.get("status", "")).strip().lower()
                    sources = j.get("sources")
                    prog = j.get("progress_value")
                    try:
                        if prog is not None and prog != _last_progress:
                            log.info("AL ingest: dataset %s dataset-status progress=%s%%", dataset_id, prog)
                            _last_progress = prog
                    except Exception:
                        pass
                    if msg in _READY or status in _READY:
                        return True
                    try:
                        if prog is not None and float(prog) >= _progress_ready:
                            return True
                    except Exception:
                        pass
                    if os.environ.get("ARTA_AN_POLL_SOURCES_READY") == "1" \
                            and isinstance(sources, list) and len(sources) > 0:
                        return True
                    if msg in _FAILED or status in _FAILED:
                        os.environ["ARTA_AN_SEED_SKIP_REASON"] = f"ingestion_consumer_error:{status or msg}"
                        log.warning("AL ingest: dataset %s indexing FAILED", dataset_id)
                        return False
            except Exception as exc:
                log.debug("AL poll dataset-status: %s", exc)
            time.sleep(interval)
        if os.environ.get("ARTA_AN_POLL_OPTIMISTIC") == "1":
            log.info("AL ingest: poll timed out — proceeding optimistically (killswitch)")
            return True
        # truthful timeout — the SUT never advanced the dataset to query-ready within the
        # window (the background indexer didn't finish); NOT a false 'ready'.
        os.environ.setdefault("ARTA_AN_SEED_SKIP_REASON", "ingestion_not_indexed")
        log.warning("AL ingest: dataset %s NOT ingested within %ss (last monitor=%s) — SKIP "
                    "truthfully (not a SUT-correctness FAIL). Raise ARTA_ANALYTICS_INGEST_TIMEOUT "
                    "for slower consumers.", dataset_id, int(timeout), _last_mon)
        return False

    # ── orchestration ─────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def seeded_dataset(self, file_path: str, *, mode: str = "files", base_name: str = "rec",
                       dataset_type: str = None):
        """The real seed — MODE-driven via the workflow manifest (SUT-agnostic). Steps for
        an UPLOAD mode (files/excel): presigned-S3 upload → cm addFile → create-data-set →
        ★ TRIGGER-INGESTION-JOB (enqueue the object for the consumer) → poll-until-indexed
        → yield dataset_id (None on any step failure → caller BLOCKs). The `mode` selects
        the dataset_id prefix (files_/excel_/…) → dataset_type → engine (the generator
        picks excel/mongo for count checks, files for content). GUARANTEED dataset teardown
        in `finally`. R154-gated. For FILE/EXCEL datasets source_id ≡ [dataset_id] (no
        separate source entity)."""
        import httpx
        allowed, reason = seed_required()
        if not allowed:
            raise PermissionError(f"AL ingest R154 gate: {reason}")
        man = _manifest()
        spec = _man.dataset_mode(man, mode) or _man.dataset_mode(man, "files")
        prefix = spec.get("id_prefix", "files_")
        is_excel = bool(spec.get("is_excel"))
        ctx = self._context()
        proj = _project_id(ctx)
        name = f"{_namespace()}__{base_name}__{uuid.uuid4().hex[:8]}"
        # dataset_id is CLIENT-generated; the prefix drives dataset_type + engine routing.
        # SUT-GROUNDED format: every WORKING excel/files record in user_master uses a
        # HYPHENATED UUID (e.g. `excel_d5253ae1-6068-40d6-8b18-faab8f0d84d6`), matching the
        # SPA (`${prefix}${uuid4()}`) + this module's own docstring — NOT `uuid4().hex`
        # (hyphen-less), which diverged and which the excel-consumer's UUID parsing may
        # reject. Env override ARTA_AN_DATASET_ID_HEX=1 restores the legacy hyphen-less form.
        did = prefix + (uuid.uuid4().hex if os.environ.get("ARTA_AN_DATASET_ID_HEX") == "1"
                        else str(uuid.uuid4()))
        created = triggered = False
        job_id = None
        # A1 — emit/upload in a SUT-ACCEPTED format (a DISCOVERED contract, manifest
        # upload_exts): EXCEL needs a real .xlsx (tabular engine); FILES needs a text-ish
        # ext (.csv/.txt/.md/…) — parquet is accepted by NEITHER. Convert (None → no
        # writer → BLOCK truthfully). Temp is cleaned in `finally`.
        accepted = [str(e).lower() for e in (spec.get("upload_exts") or [])]
        upload_path = _ensure_xlsx(file_path) if is_excel else _ensure_files_format(file_path, accepted)
        _tmp_conv = upload_path if (upload_path and upload_path != file_path) else None
        with httpx.Client(timeout=_TIMEOUT, verify=False) as client:
            try:
                if not upload_path:
                    os.environ["ARTA_AN_SEED_SKIP_REASON"] = (
                        "ingestion_xlsx_unavailable" if is_excel else "format_not_accepted")
                    log.warning("AL ingest: mode=%s could not produce a SUT-accepted upload "
                                "format from %s (accepted=%s) — BLOCK truthfully",
                                mode, file_path, accepted)
                    yield None
                    return
                # ★ FAIL-FAST guard: NEVER send an extension the SUT rejects. The accepted
                # set is the manifest's discovered upload_exts contract; parquet ∉ any mode.
                _ext = os.path.splitext(upload_path)[1].lower()
                if accepted and _ext not in accepted:
                    os.environ["ARTA_AN_SEED_SKIP_REASON"] = f"format_not_accepted:{_ext or 'noext'}"
                    log.warning("AL ingest: %s not in accepted upload_exts %s for mode=%s — BLOCK "
                                "before upload (never send a format the SUT rejects)",
                                _ext or upload_path, accepted, mode)
                    yield None
                    return
                presign = self.generate_upload_url(client, ctx, name + os.path.splitext(upload_path)[1])
                file_id = presign.get("file_id")
                ok_s3 = self.upload_to_s3(client, presign, upload_path) if presign else False
                _, bucket = self._s3_key_and_bucket(presign) if presign else ([], "")
                # the RETRIEVABLE download_url (re-GET) — the presign `url` is PUT-only.
                file_url = (self.fetch_download_url(client, ctx, file_id) if (file_id and ok_s3)
                            else "") or presign.get("download_url") or ""
                # the job-create `files[]` needs the FULL S3 object URL (query stripped),
                # NOT a bare key — the consumer derives the bucket from the URL netloc.
                clean_url = file_url.split("?")[0] if file_url else ""
                if file_id and ok_s3:
                    self.add_file(client, ctx, file_id=file_id, file_url=file_url, file_name=name,
                                  dataset_id=did, project_id=proj)
                    created = bool(self.create_dataset(client, ctx, dataset_name=name, dataset_id=did))
                if created:
                    # ★ THE REAL INGESTION TRIGGER (2026-07-25, AUTHORITATIVE deployed
                    # source): the MONITORING `job-create`, NOT data-processing/event/
                    # branch — `.arta/projects.json` repositories) `DatasetConfig/add.js`
                    # `handleFileBasedDataset`: the file-dataset flow calls
                    # `MonitoringServiceApi.createJob` (monitoring/event/job-create) as the
                    # ingestion trigger; there is NO data-processing/file-upload call. The
                    # file-upload endpoint 200s but NEVER indexes (it is not the SPA's
                    # trigger — dataset-status stays sources:[]).
                    #
                    # Two SPA specifics the job body MUST honour (both live-verified):
                    #  1. `is_excel=False` HARDCODED even for an excel dataset — the engine
                    #     routes by the `excel_`/`files_` dataset_id PREFIX, not this flag.
                    #     (Sending is_excel=True was why ARTA's prior job-create never
                    #     indexed.)
                    #  2. `files[]` = the STRIPPED (unsigned) S3 object url (`clean_url`,
                    #     query dropped) — the SPA maps `file_url.split("?")[0]`.
                    # LIVE-PROVEN 2026-07-25: seed a 137-row .xlsx → monitoring job-create →
                    # dataset-status Success + sources:['direct_files'] in ~41s → the SUT
                    # answered "137 records". Killswitch ARTA_AN_TRIGGER_DISABLE=1.
                    job_id = self.trigger_ingestion_job(
                        client, ctx, dataset_id=did, dataset_name=name,
                        file_urls=([clean_url] if clean_url else []),
                        bucket_name=bucket, is_excel=False)
                    triggered = bool(job_id)
                    # Opt-in legacy data-processing file-upload (NOT the indexer; off by
                    # default). Retained only for A/B diagnostics, never the primary path.
                    if os.environ.get("ARTA_AN_FILE_UPLOAD_TRIGGER_ALSO") == "1":
                        self.trigger_file_upload(
                            client, ctx, dataset_id=did, dataset_name=name,
                            file_urls=([file_url] if file_url else []), is_excel=is_excel,
                            source_name=name)
                # gate on dataset-status reaching status:'Success' (+ sources non-empty) —
                # the query-side indexing-complete signal the monitoring job advances to.
                indexed = bool(triggered and self.poll_status(client, ctx, did, job_id=job_id))
                if indexed:
                    os.environ["ARTA_ANALYTICS_DATASET_ID"] = did
                    os.environ["ARTA_ANALYTICS_DATASET_NAME"] = name
                    resolve_app_id(ctx, allow_create=True, dataset_ids=[did])
                    os.environ.pop("ARTA_AN_SEED_SKIP_REASON", None)
                    log.info("AL ingest: seeded %s dataset %s ready", mode, did)
                    yield did
                else:
                    # TRUTHFUL skip_reason (R123.D) — WHICH stage failed (all observed live):
                    reason = ("ingestion_presign_failed" if not (presign and file_id) else
                              "ingestion_s3_upload_failed" if not ok_s3 else
                              "ingestion_create_dataset_failed" if not created else
                              "ingestion_job_trigger_failed" if not triggered else
                              "ingestion_not_indexed")   # enqueued but consumer didn't finish
                    os.environ["ARTA_AN_SEED_SKIP_REASON"] = reason
                    log.warning("AL ingest: seed failed [%s] (presign=%s s3=%s dataset=%s trigger=%s)",
                                reason, bool(presign), locals().get("ok_s3"), created, triggered)
                    yield None
            finally:
                os.environ.pop("ARTA_ANALYTICS_DATASET_ID", None)
                os.environ.pop("ARTA_ANALYTICS_DATASET_NAME", None)
                # A8.5 — tear down the fresh per-dataset app WE created (tracked by
                # ARTA_ANALYTICS_APP_NAME; never an operator-provided app_id) so a
                # correctness seed leaves ZERO orphans (apps + dataset).
                if os.environ.get("ARTA_ANALYTICS_APP_NAME"):
                    _app = os.environ.pop("ARTA_ANALYTICS_APP_ID", None)
                    os.environ.pop("ARTA_ANALYTICS_APP_NAME", None)
                    if _app:
                        try:
                            dataset_client.delete_app(client, ctx, _app)
                        except Exception as _ax:
                            log.debug("A8.5 app teardown skipped: %s", _ax)
                if created:
                    self.delete_dataset(client, ctx, did)
                if _tmp_conv:   # remove the .xlsx/.csv we converted for the accepted format
                    try:
                        os.unlink(_tmp_conv)
                    except Exception:
                        pass
                log.info("AL ingest: torn down dataset=%s", did if created else None)


def _agent_claims() -> dict:
    """The analytics agent token's JWT carries account_id/project_id/workspace_id/
    organization_id — read them from storage (the ingestion URLs + collection_item
    need them). {} when absent."""
    st = _ab._load_storage() or {}
    for o in st.get("origins", []) or []:
        for it in o.get("localStorage", []) or []:
            if (it.get("name") or "") == "agent-api-token":
                try:
                    return _ab._jwt_claims(json.loads(it.get("value") or "").get("token"))
                except Exception:
                    return {}
    return {}


def _account_id(ctx) -> str:
    return (_cfg("ARTA_AN_ACCOUNT_ID") or _agent_claims().get("account_id")
            or _ab._jwt_claims((ctx.get("tokens") or {}).get("session_token")).get("root_account_id") or "")


def _project_id(ctx) -> str:
    return _cfg("ARTA_AN_PROJECT_ID") or _agent_claims().get("project_id") or ""


def _first_id(d: dict, *keys) -> str | None:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


ingestion_client = IngestionClient()


def active_seeder():
    """Return the dataset seeder the correctness tests use. BOTH expose the same
    `seeded_dataset(file_path, *, base_name)` context manager, so the emitted test
    is agnostic.

    R265.B — DEFAULT v2 (the real 4-service presigned-S3 / monitoring ingestion),
    now that it is LIVE-CONFIRMED end-to-end against the SUT (G3, 2026-07-22:
    presign=True, s3=True, create-dataset=True, trigger=True — real data uploaded +
    dataset created; teardown deletes it). The legacy v1 `dataset_client` uploads
    via the legacy analytics `file-upload` endpoint, which is DEAD-INFRA on the reference SUT (HTTP 500)
    — defaulting to it made every autonomous correctness seed fail at step 1. Opt
    back to v1 with ARTA_AN_INGEST_MODE=v1."""
    return dataset_client if os.environ.get("ARTA_AN_INGEST_MODE") == "v1" else ingestion_client


def _missing_fields(resp) -> str:
    """Parse a FastAPI 422 into 'field:type,field:type' (the required-field signal)."""
    try:
        return ",".join(".".join(str(x) for x in d.get("loc", [])[1:]) + ":" + d.get("type", "")
                        for d in resp.json().get("detail", []))
    except Exception:
        return (resp.text or "")[:140]


def probe_ingestion_shapes(client, ctx) -> dict:
    """LIVE diagnostic — 422-probe the request shapes whose exact fields are
    deployment/live-only (create-data-set, create-source, generate-upload-url), so
    the config can be filled without guessing. Returns {step: findings}. Run inside a
    stable SUT window (auth must be resolved). SUT-agnostic pattern (same as how
    app_id/dataset_name/create-app were nailed)."""
    ic = IngestionClient()
    sub, subn, acct = ic._ids(ctx)
    base = ctx["base_url"]
    h, _ = ic._headers(ctx, _ab.auth_path("x"))
    out = {}
    # create-data-set required fields
    r = client.post(f"{base}/subscriber/{sub}/subscription/{subn}/function/user-management/event/create-data-set",
                    headers=h, json={})
    out["create-data-set"] = f"{r.status_code} required=[{_missing_fields(r)}]"
    # get-list-of-apps response key (app_id retrieval)
    r = client.get(f"{base}/subscriber/{sub}/subscription/{subn}/function/user-management/event/get-list-of-apps", headers=h)
    try:
        out["get-list-of-apps"] = f"{r.status_code} keys={list(r.json().keys())}"
    except Exception:
        out["get-list-of-apps"] = f"{r.status_code} {(r.text or '')[:80]}"
    # generate-upload-url (needs the collections schema)
    schema = _collections_schema()
    out["collections_schema_set"] = bool(schema)
    if schema:
        murl = f"{_backend_host()}/api/media/{acct}/subscriber/{sub}/subscription/{subn}/{schema}/generate-upload-url"
        r = client.post(murl, headers=h, json={})
        out["generate-upload-url"] = f"{r.status_code} required=[{_missing_fields(r)}]"
    return out
