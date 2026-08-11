"""Phase C — CallChain abstraction.

The architectural addition that makes provider↔consumer modeling possible
across ARTA. Without this, the env-var harvest in Phase B is a one-shot
fix; with it, the system gains causality awareness used by Phase D
(chain-aware Newman generation), Phase E (step-level execution), Phase F
(gate sequence-integrity check), Phase G (cascade-aware defect intel +
chain-reverify healing), and Phase H (chain-view frontend).

Two data structures + one algorithm:

  CallNode   : one HTTP request observed in a HAR. Stamps `provides`
               (variables this node's response makes available) and
               `consumes` (variables this node's request reads from
               earlier nodes).

  CallChain  : an ordered sequence of CallNodes from a single Playwright
               test. `chain_id` is the deterministic hash of the endpoint
               sequence; `semantic_hash` extends it with status codes per
               DR-4 to distinguish create-success from create-failure
               variants of the same endpoint sequence.

  extract_chains_from_har : converts the redacted records returned by
               sut_onboarding._ingest_har into one CallChain per
               Playwright test boundary. The dataflow inference inside
               (private `_infer_dataflow`) is the load-bearing piece per
               the plan's DR-1 — it discovers which response field of an
               earlier call provides which input of a later call.

DR-1 algorithm details:
  - Build a `provider_index` keyed on candidate token VALUES, with a list
    of (node_idx, jsonpath, specificity) tuples per value.
  - For each consumer node, scan URL path segments + query values + request
    body sample + outgoing headers/cookies for tokens that match a value
    seen earlier.
  - Most recent provider wins; ties broken by jsonpath specificity (longer
    + more named segments score higher).
  - Noise filter: pure dates, very long hex blobs, and pure-numeric strings
    < 6 chars are not promoted as variable candidates — they're too generic.
  - Parallel-call tolerance (50ms burst window): nodes whose `started_at`
    timestamps are within 50ms are siblings, no provides/consumes between
    them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

log = logging.getLogger("arta.call_chain")


# ── R215 Item 3 — cm collection_id (UUID-shaped KEY) capture helpers ──────────
import os as _os_r215

_R215_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# the cm list response uses these as NON-id top-level keys; never treat as a collection id
_R215_CM_NON_ID_KEYS = {"collection", "last_evaluated_key", "published_collections_detail"}


def _R215_cm_id_disabled() -> bool:
    return _os_r215.environ.get("ARTA_R215_CM_COLLECTION_ID_DISABLE", "").lower() in ("1", "true")


def _R215_is_uuid_key(k: Any) -> bool:
    return isinstance(k, str) and str(k).lower() not in _R215_CM_NON_ID_KEYS and bool(_R215_UUID_RE.match(k))


# ── Test boundary detection ───────────────────────────────────────────────────
# Playwright HAR captures all network traffic from one process; we delineate
# per-test scope via "page.goto()" navigations OR a time gap > 5 seconds.
# In practice, ARTA runs each test in its own browser context (default), so
# every HAR file we ingest is single-test. But we keep the boundary detector
# defensive in case operators set `recordHar` on a shared context.

_TEST_BOUNDARY_GAP_MS = 5_000


# ── Noise filters (DR-1) ──────────────────────────────────────────────────────

_DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}")
_LONG_HEX_PAT = re.compile(r"^[0-9a-fA-F]{40,}$")
_TIMESTAMP_PAT = re.compile(r"^1[0-9]{12}$")  # millis-since-epoch
_SHORT_NUMERIC_PAT = re.compile(r"^\d{1,5}$")


def _is_noise_value(value: str) -> bool:
    """A token that should NOT promote to a `provides` candidate.

    Dates, very long hex blobs, raw timestamps, and short pure-numerics are
    too common across responses to carry causal meaning. Phase B4's harvest
    has the same filter; we duplicate it here so chain-extraction can run on
    HARs that bypass the harvest (e.g., Phase I5 chain replay).
    """
    if not value or not isinstance(value, str):
        return True
    if len(value) < 4:
        return True
    if _DATE_PAT.match(value):
        return True
    if _LONG_HEX_PAT.match(value):
        return True
    if _TIMESTAMP_PAT.match(value):
        return True
    if _SHORT_NUMERIC_PAT.match(value):
        return True
    if value.startswith("<<REDACTED"):
        return True
    return False


# ── Specificity scoring ───────────────────────────────────────────────────────


def _jsonpath_specificity(jp: str) -> int:
    """Longer + more named segments score higher.

    `$.org.id` (specific) beats `$.id` (generic). `$.items[0].id` beats
    `$.id` because it reaches deeper into structured response data, even
    though the generic `id` is shorter.
    """
    if not jp:
        return 0
    segments = [s for s in jp.split(".") if s and s != "$"]
    score = len(segments) * 10
    for seg in segments:
        # Named segments (alphabetic) score higher than indexed ([0])
        if seg.replace("[0]", "").replace("[*]", "").isalpha():
            score += 5
    return score


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class CallNode:
    """A single HTTP call observed in a HAR. Indices/timestamps reference
    the parent CallChain; the dataflow links (`provides` / `consumes`) are
    populated by `_infer_dataflow` after all nodes are constructed.
    """
    sequence_index: int
    method: str
    path_template: str          # `/v1/datasets/{dataset_id}` after token substitution
    concrete_url: str
    status: int
    started_at: str | None      # ISO-8601 string from HAR
    duration_ms: int

    # Shape catalog (Phase B3 stamps these via `_infer_shape`)
    request_body_shape: dict | None = None
    response_body_shape: dict | None = None

    # Dataflow links (Phase C2 stamps these via `_infer_dataflow`)
    # provides[var_name] = jsonpath into THIS node's response that yields the value.
    provides: dict[str, str] = field(default_factory=dict)
    # consumes[var_name] = sequence_index of the earlier node that provided the value.
    consumes: dict[str, int] = field(default_factory=dict)

    def endpoint_key(self) -> str:
        return f"{self.method}:{self.path_template}"


@dataclass
class CallChain:
    """An ordered sequence of CallNodes from a single test."""
    chain_id: str               # sha256 of (project, ordered method:path_template tuple)
    semantic_hash: str          # sha256 of (chain_id, ordered status sequence) — DR-4
    project_id: str
    source_test_id: str | None
    nodes: list[CallNode]
    captured_at: str
    occurrence_count: int = 1   # bumped on dedup-merge in api_discovery
    source_har: str | None = None

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "semantic_hash": self.semantic_hash,
            "project_id": self.project_id,
            "source_test_id": self.source_test_id,
            "captured_at": self.captured_at,
            "occurrence_count": self.occurrence_count,
            "source_har": self.source_har,
            "nodes": [asdict(n) for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CallChain":
        nodes = [CallNode(**n) for n in (d.get("nodes") or [])]
        return cls(
            chain_id=d["chain_id"],
            semantic_hash=d.get("semantic_hash", d["chain_id"]),
            project_id=d.get("project_id", ""),
            source_test_id=d.get("source_test_id"),
            nodes=nodes,
            captured_at=d.get("captured_at", ""),
            occurrence_count=d.get("occurrence_count", 1),
            source_har=d.get("source_har"),
        )

    def endpoint_keys(self) -> list[str]:
        return [n.endpoint_key() for n in self.nodes]


# ── Public extraction API ─────────────────────────────────────────────────────


def extract_chains_from_har(
    records: list[dict],
    *,
    project_id: str,
    source_test_id: str | None = None,
    source_har: str | None = None,
    known_ids: dict[str, str] | None = None,
) -> list[CallChain]:
    """Convert HAR records (output of `sut_onboarding._ingest_har`) into chains.

    Records are expected to be already-redacted (Phase I1) and already-
    capped (Phase I2). The function never raises on malformed input — it
    skips records that don't have a usable URL and continues.
    """
    if not records:
        return []

    # Group records by test boundary. For now we treat the whole HAR as one
    # chain unless we see a > 5s gap (strong heuristic for a new test).
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_started: datetime | None = None

    for rec in records:
        if not isinstance(rec, dict):
            continue
        url = rec.get("path") or rec.get("url") or ""
        if not url:
            continue
        started_at = _parse_iso(rec.get("started_at"))
        if last_started and started_at and (started_at - last_started).total_seconds() * 1000 > _TEST_BOUNDARY_GAP_MS:
            if current:
                groups.append(current)
                current = []
        current.append(rec)
        last_started = started_at or last_started
    if current:
        groups.append(current)

    chains: list[CallChain] = []
    for group in groups:
        chain = _build_chain(
            group,
            project_id=project_id,
            source_test_id=source_test_id,
            source_har=source_har,
            known_ids=known_ids,
        )
        if chain.nodes:
            chains.append(chain)
    return chains


def _build_chain(
    records: list[dict],
    *,
    project_id: str,
    source_test_id: str | None,
    source_har: str | None,
    known_ids: dict[str, str] | None = None,
) -> CallChain:
    nodes: list[CallNode] = []
    for idx, rec in enumerate(records):
        method = (rec.get("method") or "GET").upper()
        url = rec.get("url") or ""
        path = rec.get("path") or url

        # Build path_template via segment scan — same heuristic as
        # api_discovery.harvest_envvars_from_har for consistency.
        path_template = _path_to_template(path, _segment_kind_lookup(records), known_ids)

        nodes.append(CallNode(
            sequence_index=idx,
            method=method,
            path_template=path_template,
            concrete_url=url,
            status=int(rec.get("status") or 0),
            started_at=rec.get("started_at"),
            duration_ms=int(rec.get("duration_ms") or 0),
            request_body_shape=rec.get("request_body_shape"),
            response_body_shape=rec.get("response_body_shape"),
        ))

    # R214 N2 — dedupe redundant idempotent GET reads. The discovery HAR
    # re-fetches the same cm collection lists many times (SPA re-navigation) →
    # 100+ identical-shape GET nodes dumped into ONE chain (e.g. the
    # `req_am_001_chain` with 100+ cm reads) → bloated specs + repeated grounding
    # violations. Collapse duplicate (method, path_template) GET nodes, keeping
    # the first; mutations (non-GET) are NEVER deduped (they may legitimately
    # repeat). Killswitch ARTA_R214_CHAIN_DEDUP_DISABLE=1.
    import os as _os_n2
    if _os_n2.environ.get("ARTA_R214_CHAIN_DEDUP_DISABLE", "").lower() not in ("1", "true"):
        _seen_get: set = set()
        _deduped: list[CallNode] = []
        for _n in nodes:
            if (_n.method or "GET").upper() == "GET":
                _key = (_n.method, _n.path_template)
                if _key in _seen_get:
                    continue
                _seen_get.add(_key)
            _deduped.append(_n)
        if len(_deduped) < len(nodes):
            for _ri, _dn in enumerate(_deduped):
                _dn.sequence_index = _ri
            nodes = _deduped

    # DR-1 dataflow inference. Modifies `provides`/`consumes` in place on
    # the nodes. Pass the original records (with body samples) — the nodes
    # don't carry samples to keep the persisted chain small.
    _infer_dataflow(nodes, records)

    chain_id = _compute_chain_id(project_id, nodes)
    semantic_hash = _compute_semantic_hash(chain_id, nodes)
    return CallChain(
        chain_id=chain_id,
        semantic_hash=semantic_hash,
        project_id=project_id,
        source_test_id=source_test_id,
        nodes=nodes,
        captured_at=records[0].get("started_at") or "",
        source_har=source_har,
    )


# ── Dataflow inference (DR-1, the load-bearing piece) ────────────────────────


def _infer_dataflow(nodes: list[CallNode], records: list[dict]) -> None:
    """Match consumer-side tokens to earlier provider responses.

    Algorithm:
      1. For each node, walk its response body sample (when available) and
         emit (jsonpath, value) pairs for every leaf string. Record these
         as provider candidates indexed by value.
      2. For each consumer node N, extract candidate tokens from:
           - URL path segments (the ones substituted as path templates)
           - URL query values
           - request body sample leaf strings
           - outgoing header values (Authorization etc — already redacted but
             cookies / Set-Cookie tokens can flow this way)
      3. Match each consumer token to providers from earlier nodes (idx < N).
         Most recent wins; ties broken by jsonpath specificity.
      4. Stamp `nodes[provider_idx].provides[var_name] = jsonpath` and
         `nodes[N].consumes[var_name] = provider_idx`.
    """
    if not nodes or len(nodes) < 2:
        return

    # Build provider index: value → list of (node_idx, jsonpath, specificity).
    # We scan response bodies, response Set-Cookies, and explicit response IDs.
    provider_index: dict[str, list[tuple[int, str, int]]] = {}
    for idx, rec in enumerate(records):
        if idx >= len(nodes):
            break
        sample = rec.get("response_body_sample")
        for jsonpath, value in _walk_leaves(sample):
            if not isinstance(value, str) or _is_noise_value(value):
                continue
            provider_index.setdefault(value, []).append(
                (idx, jsonpath, _jsonpath_specificity(jsonpath))
            )
        # Set-Cookies are a parallel data-flow lane (auth tokens often live there)
        for cookie in rec.get("set_cookies") or []:
            cval = cookie.get("value") if isinstance(cookie, dict) else None
            if isinstance(cval, str) and not _is_noise_value(cval):
                provider_index.setdefault(cval, []).append(
                    (idx, f"$cookie.{cookie.get('name', '?')}", _jsonpath_specificity(f"cookie.{cookie.get('name', '?')}"))
                )

    # Burst-window detection (DR-1): nodes with started_at deltas ≤ 50ms are
    # siblings. We don't infer provides/consumes between them.
    burst_buddies = _compute_burst_groups(nodes)

    # Resolve consumer tokens.
    for j, node in enumerate(nodes):
        if j >= len(records):
            break
        rec = records[j]

        consumer_tokens: list[tuple[str, str]] = []  # (var_name_hint, value)

        # 2a — URL path tokens. The `path_template` already names them
        # (`/datasets/{dataset_id}` → `dataset_id`); the concrete value lives
        # in the original URL. We pair them by walking segments together.
        consumer_tokens.extend(_extract_path_tokens(node.concrete_url, node.path_template))

        # 2b — Query string values. `?token=...` etc.
        for q in rec.get("query_params") or []:
            qname = q.get("name") if isinstance(q, dict) else None
            qval = q.get("value") if isinstance(q, dict) else None
            if qname and isinstance(qval, str) and not _is_noise_value(qval):
                consumer_tokens.append((str(qname), qval))

        # 2c — request body leaf strings.
        for jp, value in _walk_leaves(rec.get("request_body_sample")):
            if isinstance(value, str) and not _is_noise_value(value):
                # Use last jsonpath segment as the variable-name hint.
                hint = jp.rsplit(".", 1)[-1] if "." in jp else jp
                consumer_tokens.append((hint, value))

        # 2d — outgoing cookies (e.g., `Cookie: session=abc`). These usually
        # carry session tokens that flowed FROM a prior login response.
        for cookie in rec.get("cookies") or []:
            cname = cookie.get("name") if isinstance(cookie, dict) else None
            cval = cookie.get("value") if isinstance(cookie, dict) else None
            if cname and isinstance(cval, str) and not _is_noise_value(cval):
                consumer_tokens.append((f"cookie_{cname}", cval))

        # 3+4 — match to earlier providers, stamp links.
        seen_var_names: set[str] = set()
        for hint, value in consumer_tokens:
            candidates = provider_index.get(value, [])
            # Filter to STRICTLY earlier providers AND not in this consumer's burst window.
            valid = [
                (i, jp, sp) for (i, jp, sp) in candidates
                if i < j and j not in burst_buddies.get(i, set())
            ]
            if not valid:
                continue
            # Most recent provider wins; ties broken by specificity then jsonpath length.
            valid.sort(key=lambda t: (t[0], t[2], len(t[1])))
            provider_idx, jsonpath, _ = valid[-1]

            var_name = _canonicalize_var_name(hint, jsonpath)
            if var_name in seen_var_names:
                continue   # one var per consumer slot — first match wins
            seen_var_names.add(var_name)

            node.consumes[var_name] = provider_idx
            nodes[provider_idx].provides[var_name] = jsonpath


# ── Helpers ───────────────────────────────────────────────────────────────────


def _walk_leaves(obj: Any, prefix: str = "$") -> list[tuple[str, Any]]:
    """Walk a JSON tree and yield (jsonpath, leaf_value) pairs.

    Bounded depth (5) to keep huge responses tractable. First-array-element
    only — `$.items[0].id` rather than walking every element. Phase C
    consumers only need ONE candidate per response field.
    """
    out: list[tuple[str, Any]] = []
    _walk_leaves_inner(obj, prefix, out, depth=0)
    return out


def _walk_leaves_inner(obj: Any, prefix: str, out: list[tuple[str, Any]], *, depth: int) -> None:
    if depth >= 5 or obj is None:
        return
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:50]:   # cap fan-out
            # R215 Item 3 — cm collection_id is a UUID-shaped KEY, not a field:
            # the cm list response is {"Collection":…, "<collection-uuid>":[…]}.
            # _walk_leaves normally emits only VALUES, so the collection id (the
            # key) is invisible to dataflow inference → the child node's
            # {collection_id} is never linked → unresolved → 404. Emit the
            # UUID-key's VALUE (so _infer_dataflow matches it to the child's
            # consumed id) at a SENTINEL jsonpath (`$.__cm_collection_key__`) the
            # Newman capture template recognizes to scan-for-key at runtime
            # (the key is dynamic per run, so a literal `$.<uuid>` jsonpath
            # wouldn't generalize). Killswitch ARTA_R215_CM_COLLECTION_ID_DISABLE=1.
            if (isinstance(v, list)
                    and _R215_is_uuid_key(k)
                    and not _R215_cm_id_disabled()):
                out.append(("$.__cm_collection_key__", k))
            _walk_leaves_inner(v, f"{prefix}.{k}", out, depth=depth + 1)
    elif isinstance(obj, list):
        if obj:
            _walk_leaves_inner(obj[0], f"{prefix}[0]", out, depth=depth + 1)
    elif isinstance(obj, (str, int, float, bool)):
        out.append((prefix, obj))


def _extract_path_tokens(concrete_url: str, path_template: str) -> list[tuple[str, str]]:
    """Pair `{var}` placeholders in `path_template` with concrete values in
    `concrete_url`. Returns [(var_name, concrete_value), ...].
    """
    if not concrete_url or not path_template:
        return []
    # Strip query and host from concrete_url.
    if "://" in concrete_url:
        try:
            from urllib.parse import urlparse
            concrete_url = urlparse(concrete_url).path or concrete_url
        except Exception:
            pass
    if "?" in concrete_url:
        concrete_url = concrete_url.split("?", 1)[0]

    template_segments = [s for s in path_template.split("/") if s]
    concrete_segments = [s for s in concrete_url.split("/") if s]
    if len(template_segments) != len(concrete_segments):
        return []
    out: list[tuple[str, str]] = []
    for t_seg, c_seg in zip(template_segments, concrete_segments):
        if t_seg.startswith("{") and t_seg.endswith("}"):
            var_name = t_seg[1:-1]
            if c_seg and not _is_noise_value(c_seg):
                out.append((var_name, c_seg))
    return out


# Same heuristics as api_discovery — duplicated locally to avoid the
# circular dependency call_chain ↔ api_discovery.
_PATH_KIND_STOPWORDS = {
    "api", "v1", "v2", "v3", "v4", "graphql", "rest", "rpc",
    "auth", "login", "logout", "session", "oauth",
}
_ID_LIKE_PAT = re.compile(
    r"^("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9A-HJ-NP-Z]{20,30}"
    r"|[a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*"
    r"|\d{6,20}"
    r")$"
)
_PATH_NOUN_PAT = re.compile(r"^[a-z]{2,12}$")


def _looks_like_id(seg: str) -> bool:
    if not seg or len(seg) < 4 or seg.lower() in _PATH_KIND_STOPWORDS:
        return False
    if _PATH_NOUN_PAT.match(seg):
        return False
    return bool(_ID_LIKE_PAT.match(seg))


def _segment_kind_lookup(records: list[dict]) -> dict[str, str]:
    """Build a `{concrete_value: kind_hint}` map across all records so
    multiple chains agree on path-template var names.

    Example: across 5 records that hit `/datasets/abc123` and
    `/datasets/abc123/snapshots/xyz`, the value `abc123` always pairs with
    kind `datasets` (→ var name `dataset_id`).
    """
    out: dict[str, str] = {}
    for rec in records:
        path = rec.get("path") or rec.get("url") or ""
        if "://" in path:
            try:
                from urllib.parse import urlparse
                path = urlparse(path).path or path
            except Exception:
                pass
        if "?" in path:
            path = path.split("?", 1)[0]
        segments = [s for s in path.split("/") if s]
        for i, seg in enumerate(segments):
            if _looks_like_id(seg):
                kind = segments[i - 1] if i > 0 else "id"
                out.setdefault(seg, kind)
    return out


def _path_to_template(path: str, kind_lookup: dict[str, str],
                      known_ids: dict[str, str] | None = None) -> str:
    """Rewrite a concrete URL path to its `{var}` template form.

    B1 — when a concrete id segment's VALUE matches a known project env-var id
    (e.g. organizations[0]=424e744f… → `organization_id`, root_account_id →
    `account_id`, subscriber_id, subscription_id), template it to THAT var so
    the URL resolves to the CORRECT id at runtime. Without this, the segment is
    named from the preceding path word (e.g. `/media/424e744f…` → `{media_id}`),
    which has no env var → unresolved → R43 fabricates a wrong UUID → SUT 500.
    The value-match (known_ids) wins over the positional kind heuristic.
    """
    if "://" in path:
        try:
            from urllib.parse import urlparse
            path = urlparse(path).path or path
        except Exception:
            pass
    if "?" in path:
        path = path.split("?", 1)[0]
    segments = [s for s in path.split("/") if s]
    # R214 N1 — collection-manager account-slot fix (mirrors api_discovery
    # leading slot and the slot after `collection`; pre-R214 the kind heuristic
    # produced `{collection_id}`/`{id_id}` which don't resolve → 404 + grounding
    # explosions. Force those two slots to account_id (independent of whether the
    # account UUID happens to be in known_ids at chain-extract time).
    # Killswitch ARTA_R214_CM_PATH_FIX_DISABLE=1.
    import os as _os_n1
    _r214_cm = (
        _os_n1.environ.get("ARTA_R214_CM_PATH_FIX_DISABLE", "").lower() not in ("1", "true")
        and "/api/collection/" in path and "/cm/" in path
    )
    # R214 N1 — leading cm segment IS the account id; use its VALUE to
    # disambiguate the `/collection/{id}` slot (same value → account_id; a
    # different id → a GENUINE collection_id that must keep its own var, NOT be
    # forced to account_id).
    _r214_acct_val = (segments[0] if (_r214_cm and segments and _looks_like_id(segments[0]))
                      else None)
    out_segs: list[str] = []
    for i, seg in enumerate(segments):
        if _looks_like_id(seg):
            if known_ids and seg in known_ids:
                out_segs.append("{%s}" % known_ids[seg])
                continue
            if _r214_cm and i == 0:
                out_segs.append("{account_id}")
                continue
            if (_r214_cm and i > 0 and segments[i - 1].lower() == "collection"
                    and _r214_acct_val and seg == _r214_acct_val):
                out_segs.append("{account_id}")
                continue
            kind = kind_lookup.get(seg, "id")
            singular = kind.lower().rstrip("s") if kind.lower().endswith("s") else kind.lower()
            var_name = f"{singular}_id"
            out_segs.append("{%s}" % var_name)
        else:
            out_segs.append(seg)
    return "/" + "/".join(out_segs) if out_segs else "/"


def _canonicalize_var_name(hint: str, jsonpath: str) -> str:
    """Pick a stable variable name for a (consumer hint, provider jsonpath) pair.

    Consumer hint comes from path-segment scan, query name, or last-segment
    of body jsonpath. The hint is the user-visible name (matches the
    project's env var conventions); the jsonpath is the locator for
    extraction. We prefer the hint unless it's a generic "id"/"value"/"name",
    in which case we fall back to the jsonpath's parent segment.
    """
    hint_lower = (hint or "").lower()
    if hint_lower in {"id", "value", "name", "v"} and jsonpath:
        # Use parent segment of jsonpath: `$.org.id` → `org_id`
        parts = [p for p in jsonpath.split(".") if p and p != "$"]
        if len(parts) >= 2 and parts[-1].lower() == "id":
            return f"{parts[-2].lower()}_id"
    return hint or "unknown_var"


def _compute_burst_groups(nodes: list[CallNode]) -> dict[int, set[int]]:
    """Detect parallel calls within 50ms windows.

    Returns {node_idx: {sibling_idx, sibling_idx, ...}}. Siblings cannot
    provide/consume to/from each other.
    """
    out: dict[int, set[int]] = {i: set() for i in range(len(nodes))}
    starts: list[tuple[int, datetime | None]] = [(i, _parse_iso(n.started_at)) for i, n in enumerate(nodes)]
    for i, ti in starts:
        if ti is None:
            continue
        for j, tj in starts:
            if i == j or tj is None:
                continue
            if abs((ti - tj).total_seconds()) * 1000 <= 50:
                out[i].add(j)
    return out


def _parse_iso(text: Any) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        # HAR uses ISO 8601 with optional trailing Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _compute_chain_id(project_id: str, nodes: list[CallNode]) -> str:
    """sha256 of (project_id, ordered (method, path_template) tuple).

    Two HARs that exercise the same logical chain produce the same chain_id;
    occurrence_count tracks duplicate observations on the persistence side.
    """
    payload = json.dumps({
        "project": project_id,
        "endpoints": [(n.method, n.path_template) for n in nodes],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _compute_semantic_hash(chain_id: str, nodes: list[CallNode]) -> str:
    """DR-4: distinguish create-success from create-failure variants.

    Two chains with the same endpoint sequence but different status sequences
    represent semantically different scripts of intent (happy-path vs
    adversarial vs error-recovery). They share `chain_id` but differ in
    `semantic_hash`.
    """
    statuses = [n.status for n in nodes]
    payload = f"{chain_id}|{json.dumps(statuses)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


__all__ = [
    "CallNode",
    "CallChain",
    "extract_chains_from_har",
]
