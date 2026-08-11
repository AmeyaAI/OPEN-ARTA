"""R250 — the REAL-ID store: test DATA grounding.

ARTA grounds endpoints (R98.3), selectors (R47.1b) and auth (R244) against the
real SUT, but has never grounded test DATA. The LLM was handed real endpoints,
real selectors and real auth, told to "use real data values (not
<placeholders>)" — and given nothing real to use. So it invented: run-0c19e6
had 33 of 63 specs hardcoding fabricated ids (`ASSET-VEH-2468`,
`ACC-OTP-57291`, `550e8400-…`), and only 4 chaining a real one. Those literals
became 520 404s, the single largest failure cluster.

The values ARTA needs were already flowing through the pipeline and being
thrown away:
  • `sut_onboarding` records carry `response_body_sample` (the real payload)
    but only `_infer_shape` (types) was ever persisted — values discarded.
  • R230 (`execution.py`) probes LIST endpoints for real ids AT DISPATCH, then
    lets them die with the run instead of caching them for GEN.

This module is the missing store. Keyed by `api_discovery._r212_skel` so it
joins `captured_endpoints` exactly as R212's response-shape store does.

Sibling of `.arta/discovered_response_shapes/` (types) — this is the values.

Killswitch: ARTA_R250_REAL_ID_STORE_DISABLE=1 → load/persist become no-ops and
every consumer degrades to its pre-R250 behavior.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("arta.real_id_store")

_REAL_IDS_DIR = Path(".arta/discovered_real_ids")

# An id-shaped FIELD NAME, snake_case or camelCase: id, account_id, assetId,
# accountGuid, order_number, product_code. R175's `is_id_var` misses camelCase
# — the SUT under test (a Java/Angular stack) emits camelCase throughout.
#
# `key` is deliberately NOT here. It reads id-ish but in practice names
# concurrency tokens, partition keys and credentials far more often than entity
# `update_key` (an etag), which would then be offered to the LLM as a usable
# id and produce fabricated-id failures by another route. `api_key`/`access_key`
# are additionally caught by _SECRET_KEY_RE, but this store is prompt-injected,
# so the shape itself stays narrow — identifiers only.
#
# `code`/`number` are accepted only when QUALIFIED (`product_code`,
# `order_number`, `assetCode`). A BARE `code`/`number` is a status/result field
# out of a response envelope and filed it under the `account` entity, which
# would have handed the LLM `/accounts/0` as a "real id".
_ID_KEY_RE = re.compile(r"(?:^|_)(id|guid|uuid)$|(?:^|_)\w+_(code|number)$", re.I)
_CAMEL_ID_KEY_RE = re.compile(r"[a-z0-9](?:Id|Guid|Uuid)$|[a-z0-9]\w+(?:Code|Number)$")

# A value the HAR redactor already replaced. har_redactor runs BEFORE this
# harvest happily filed `<<REDACTED_TOKEN>>` and `<<REDACTED_CC>>-<<REDACTED_CC>>`
# as real ids. Injecting those into a prompt as ground truth is a guaranteed
# 404 at best; treating a redaction marker as data is nonsense at worst.
_REDACTED_VALUE_RE = re.compile(
    r"redact|<<|\*{3,}|\[hidden\]|xxxxx|placeholder", re.IGNORECASE)

# Entities that are FILES, not resources (`builddata.json`, `guide.json`).
_STATIC_ASSET_RE = re.compile(r"\.(json|js|css|html|map|txt|xml|ico|png|svg)$", re.I)

# A real entity id is never the zero/sentinel value.
_SENTINEL_ID_VALUES = {"0", "-1", "null", "none", "undefined", "false", "true", ""}

# An id-shaped name is not automatically safe to persist: `api_key`,
# `secret_key` and `auth_token` all match `_key$`/id-ish shapes. This store is
# written to disk and injected into LLM prompts, so a credential must never
# reach it. Fail CLOSED — when a name looks secret-ish, drop it.
_SECRET_KEY_RE = re.compile(
    r"secret|password|passwd|pwd|token|auth|credential|private|salt|"
    r"signature|session|cookie|bearer|api_?key|access_?key",
    re.I,
)

# A JWT or long opaque blob is a credential, not a business id.
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+$")

_MAX_ID_LEN = 128


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_KEY_RE.search(name or ""))


def _is_id_name(name: str) -> bool:
    if not name or _is_secret_name(name):
        return False
    return bool(_ID_KEY_RE.search(name) or _CAMEL_ID_KEY_RE.search(name))


def _is_usable_id_value(value) -> bool:
    """A real id is a short, non-sentinel scalar. Reject blobs, JWTs, booleans,
    nulls, zero/sentinel values, and anything the HAR redactor already
    replaced."""
    if isinstance(value, bool) or value is None:
        return False
    if not isinstance(value, (str, int)):
        return False
    s = str(value).strip()
    if not s or len(s) > _MAX_ID_LEN:
        return False
    if s.lower() in _SENTINEL_ID_VALUES:
        return False
    if _JWT_RE.match(s):
        return False
    if _REDACTED_VALUE_RE.search(s):
        return False
    return True


def _singularize(word: str) -> str:
    """Heuristic, deliberately shallow: entity names come from URL segments
    (`/accounts` → `account`). Wrong for irregular plurals, which only costs a
    prompt-block label, never correctness of a value."""
    w = (word or "").strip("/").lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


# `/AccountManagement/api/GetAccountById`, so the raw last segment is a VERB
# PHRASE, not a resource. Live evidence — the first harvest produced entities
# `getaccountbyid` / `getaccounthierarchy`, which meant `real_ids_for_entity(
# "account")` returned nothing and `known_ids_for_chain` emitted
# `getaccountbyid_id` instead of the `account_id` R211 actually looks up. The
# ids were right; only their label was unusable.
_RPC_VERB_PREFIX_RE = re.compile(
    r"^(get|post|put|patch|delete|create|update|fetch|list|search|save|add|"
    r"remove|load|read|find|retrieve|query)(?=[a-z0-9])", re.IGNORECASE)
_RPC_BY_SUFFIX_RE = re.compile(r"(all|own|shared)?by[a-z0-9]*(id|guid|name|key)$", re.IGNORECASE)


def _normalize_rpc_entity(seg: str) -> str:
    """`GetAccountById` → `account`; `getAllAssetsOfAccountId` → `allassetsof…`.

    Deliberately conservative: strip a leading verb and a trailing `By<X>Id`.
    Anything it cannot confidently reduce is left as-is — a slightly odd label
    is harmless (the Gherkin filter just won't match it), whereas an
    over-aggressive rule could merge two different entities into one and hand
    the LLM the wrong id.
    """
    s = (seg or "").strip().lower()
    if not s:
        return ""
    s2 = _RPC_VERB_PREFIX_RE.sub("", s)
    s2 = _RPC_BY_SUFFIX_RE.sub("", s2)
    return _singularize(s2 or s)


def _entity_for_skeleton(skel: str) -> str:
    """Last non-wildcard segment of the path skeleton, singularized.
    `/api/v1/accounts/*` → `account`; `/AccountManagement/api/GetAccountById`
    → `account` (RPC-style).

    Returns "" for STATIC ASSETS: `/StaticAssets/buildData.json` is a
    file, not a resource, and the live harvest filed its `buildNumber` as an
    entity id under the entity `builddata.json`.
    """
    segs = [s for s in (skel or "").split("/") if s and s != "*"]
    _STRUCTURAL = {"api", "v1", "v2", "v3", "v4", "rest", "graphql", "svc"}
    for seg in reversed(segs):
        if _STATIC_ASSET_RE.search(seg):
            return ""
        if seg.lower() not in _STRUCTURAL:
            return _normalize_rpc_entity(seg)
    return ""


_ENTITY_KEY_SPLIT_RE = re.compile(r"^(.*?)_?(id|guid|uuid)$", re.IGNORECASE)


def _is_entity_key(field: str, entity: str) -> bool:
    """Is `field` the id OF `entity`, or merely an id-shaped ATTRIBUTE of it?

    THE distinction that makes this store usable. `/GetAccountById` returns
    ACCOUNT_GUID (the account's own id) alongside ZIP_CODE, OU_ID,
    BILLING_CODE, ACCOUNT_TYPE_ID and OWNER/PARENT_ACCOUNT_GUID. Grouping all
    of them under the `account` entity — which is what the first live harvest
    did — hands the LLM a zip code as an account id. `/accounts/20176` 404s
    exactly like a fabricated id, which is the failure this workstream exists
    to remove.

    Accepted:
      • bare `id` / `guid` / `uuid`         → the resource's own key
      • `<entity>_id` / `<entity>Guid`      → ACCOUNT_GUID for `account`
      • a short abbreviation of the entity  → ACCT_GUID for `account`
    Rejected (real values, but not THIS entity's key):
      • `zip_code`, `ou_id`, `billing_code` → attributes
      • `account_type_id`                   → a different entity's key
      • `owner_account_guid`, `parent_account_guid` → a RELATED account's key
    """
    f = _snake(field or "").lower()
    m = _ENTITY_KEY_SPLIT_RE.match(f)
    if not m:
        return False
    prefix = (m.group(1) or "").strip("_")
    if not prefix:
        return True                       # bare id/guid/uuid
    ent = _singularize((entity or "").lower())
    if not ent:
        return False
    if prefix == ent or _singularize(prefix) == ent:
        return True
    # Abbreviation (acct -> account): must be SHORTER than the entity and share
    # its opening. `account_type`/`owner_account` are longer -> correctly
    # rejected; `ou`/`zip` don't share the opening -> rejected.
    if 3 <= len(prefix) <= len(ent) and ent.startswith(prefix[:3]):
        return True
    return False


def _walk_leaves(node, _depth: int = 0):
    """Yield (key, value) scalar leaves. Depth-capped: response payloads can
    nest arbitrarily and this runs inside discovery."""
    if _depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _walk_leaves(v, _depth + 1)
            else:
                yield (str(k), v)
    elif isinstance(node, list):
        for item in node[:20]:
            yield from _walk_leaves(item, _depth + 1)


def _disabled() -> bool:
    return os.environ.get("ARTA_R250_REAL_ID_STORE_DISABLE", "").lower() in ("1", "true")


def extract_real_ids(records: list[dict], *, max_per_slot: int = 5) -> dict:
    """Harvest REAL id values from probe/HAR records.

    `records` are `sut_onboarding`-shaped: {method, url|path, status,
    response_body_sample}. Only 2xx GET responses are read — a real id is one
    the SUT SERVED, and a non-2xx body is an error envelope, not data.

    Returns {"<skel>::<id_field>": {entity, id_field, values[], source}}.
    """
    from .api_discovery import _r212_skel

    out: dict[str, dict] = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        status = rec.get("status") or rec.get("status_code") or 0
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        if status and status // 100 != 2:
            continue
        if str(rec.get("method") or "GET").upper() not in ("GET", ""):
            continue
        body = rec.get("response_body_sample")
        if body is None:
            continue
        path = rec.get("path") or rec.get("url") or ""
        skel = _r212_skel(str(path))
        entity = _entity_for_skeleton(skel)
        if not entity:
            # Static asset (buildData.json / guide.json) or an unusable path.
            # A file has no entity ids; harvesting its `buildNumber` and
            # offering it to the LLM as one is worse than harvesting nothing.
            continue
        source = rec.get("_source") or "probe"
        for key, value in _walk_leaves(body):
            if not _is_id_name(key) or not _is_usable_id_value(value):
                continue
            slot_key = f"{skel}::{key}"
            slot = out.setdefault(slot_key, {
                "entity": entity,
                "id_field": key,
                # Attributes are KEPT (they are real values the SUT served, so
                # R252 must not flag them as fabricated) but only entity KEYS
                # are offered to the LLM / to R211 as usable ids.
                "entity_key": _is_entity_key(key, entity),
                "values": [],
                "source": source,
            })
            val = str(value).strip()
            if val not in slot["values"] and len(slot["values"]) < max_per_slot:
                slot["values"].append(val)
    return out


def persist_real_ids(project_id: str, ids: dict, *, max_per_slot: int = 5) -> None:
    """Merge `ids` into the project's store. Union of values, capped.

    Merging (rather than overwriting) matters: R230 contributes one entity per
    dispatch while the probe contributes many at discovery, and neither should
    clobber the other.
    """
    if _disabled() or not project_id or not ids:
        return
    try:
        _REAL_IDS_DIR.mkdir(parents=True, exist_ok=True)
        existing = load_real_ids(project_id)
        for slot_key, slot in ids.items():
            if not isinstance(slot, dict):
                continue
            cur = existing.setdefault(slot_key, {
                "entity": slot.get("entity", ""),
                "id_field": slot.get("id_field", ""),
                "values": [],
                "source": slot.get("source", "probe"),
            })
            for v in (slot.get("values") or []):
                if v not in cur["values"] and len(cur["values"]) < max_per_slot:
                    cur["values"].append(v)
        path = _REAL_IDS_DIR / f"{project_id}.json"
        path.write_text(json.dumps(existing, indent=2))
        log.info(
            "R250: persisted %d real-id slots (%d values) for project %s",
            len(existing),
            sum(len(s.get("values") or []) for s in existing.values()),
            project_id,
        )
    except Exception as exc:
        # Never break discovery/dispatch over a cache write.
        log.debug("R250: persist failed for %s: %s", project_id, exc)


def load_real_ids(project_id: str) -> dict:
    """Load the project's real-id store. `{}` when absent or disabled — every
    consumer must degrade to its pre-R250 behavior on an empty store."""
    if _disabled() or not project_id:
        return {}
    try:
        path = _REAL_IDS_DIR / f"{project_id}.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("R250: load failed for %s: %s", project_id, exc)
        return {}


def real_ids_for_entity(project_id: str, entity: str) -> list[str]:
    """Real id values known for an entity type (`account` → [...]). Powers
    WS1b's [HARD CONSTRAINT — REAL TEST DATA] prompt block."""
    if not entity:
        return []
    want = _singularize(entity)
    out: list[str] = []
    for slot in load_real_ids(project_id).values():
        if not isinstance(slot, dict):
            continue
        # entity_key ONLY: an account's zip_code is a real value but is not an
        # account id, and offering it as one produces the same 404 as an
        # invented id. Older stores without the flag fall back to the check.
        if not slot.get("entity_key", _is_entity_key(
                slot.get("id_field") or "", slot.get("entity") or "")):
            continue
        if _singularize(slot.get("entity") or "") == want:
            for v in (slot.get("values") or []):
                if v not in out:
                    out.append(v)
    return out


def all_real_id_values(project_id: str) -> set[str]:
    """Flat set of every known-real id. Powers R258's fabricated-id triage and
    WS1c's grounding validator."""
    out: set[str] = set()
    for slot in load_real_ids(project_id).values():
        if isinstance(slot, dict):
            out.update(str(v) for v in (slot.get("values") or []))
    return out


_GENERIC_ID_FIELDS = {"id", "guid", "uuid", "_id"}


def _snake(name: str) -> str:
    """assetId → asset_id; AccountGUID → account_guid."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def known_ids_for_chain(project_id: str) -> dict[str, str]:
    """R250 → R211: real ids keyed by the VAR NAME a chain step expects.

    `build_spec_from_chain(known_ids=...)` takes {var_name: value}
    (`{"account_id": "ACC-9F31A2"}`). Pre-R250 its only source was
    `harvest_session_ids_from_storage` — the authenticated user's own
    org/account from the JWT — so any OTHER entity in a chain had no real id
    and the step had to discover one or skip.

    Field-name mapping:
      • a generic `id`/`guid` field  → `<entity>_id`  (accounts → account_id)
      • a specific `account_id` field → itself, snake-cased

    First value wins: a chain needs ONE real id per var, and slot values are
    insertion-ordered from the SUT's own response.
    """
    out: dict[str, str] = {}
    for slot in load_real_ids(project_id).values():
        if not isinstance(slot, dict):
            continue
        values = slot.get("values") or []
        if not values:
            continue
        # entity_key ONLY — a chain step asking for `account_id` must not be
        # handed the account's zip_code.
        if not slot.get("entity_key", _is_entity_key(
                slot.get("id_field") or "", slot.get("entity") or "")):
            continue
        entity = _singularize(slot.get("entity") or "")
        field = _snake(slot.get("id_field") or "")
        if field in _GENERIC_ID_FIELDS:
            var = f"{entity}_id" if entity else ""
        else:
            var = field
        if var and var not in out:
            out[var] = str(values[0])
        # Also expose the canonical `<entity>_id` alias, since that is the var
        # name chain steps and path params actually use (`{account_id}`), while
        # the SUT's field may be `ACCOUNT_GUID`.
        if entity:
            alias = f"{entity}_id"
            if alias not in out:
                out[alias] = str(values[0])
    return out


def entities_known(project_id: str) -> list[str]:
    """Entity types with at least one real id."""
    out: list[str] = []
    for slot in load_real_ids(project_id).values():
        if isinstance(slot, dict) and slot.get("values"):
            ent = slot.get("entity") or ""
            if ent and ent not in out:
                out.append(ent)
    return out


# ── R254.SEED (WS1e) — opt-in sandbox seeding ────────────────────────────────
#
# The default answer when no real id exists is BLOCK: R170's principle, now
# enforced against the LLM too (R252). This is the operator's explicit escape
# hatch for the case where an entity has no LIST endpoint to chain from and no
# probe ever captured one — where the honest-but-unhelpful outcome is a
# permanently BLOCKED test.
#
# It reuses R154's EXISTING gate rather than inventing a second authorization
# surface: BOTH `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` AND
# `SUT_TEST_DATA_NAMESPACE=<sandbox-id>` must be set. This is the only code path
# in WS1 that WRITES to the SUT, so it is default-off, loudly logged, and its
# ids are tagged `source="seeded"` so R258 can never credit a seeded-id failure
# as genuine SUT signal (ARTA broke its own test data; that is not a SUT defect).


def seeding_enabled() -> tuple[bool, str]:
    """R254.SEED — is opt-in seeding authorized? Returns (enabled, reason).

    Fail-closed and SILENT when unset: the default deployment must never see
    seeding warnings for a feature it did not ask for.
    """
    if os.environ.get("ARTA_R254_SEED_DISABLE", "").lower() in ("1", "true"):
        return (False, "killswitch ARTA_R254_SEED_DISABLE=1")
    if os.environ.get("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS", "").lower() not in ("1", "true"):
        return (False, "ARTA_R154_ALLOW_DESTRUCTIVE_TESTS is not set (default-deny)")
    ns = os.environ.get("SUT_TEST_DATA_NAMESPACE") or ""
    if not ns.strip():
        return (False, "SUT_TEST_DATA_NAMESPACE is not set (no sandbox scope)")
    return (True, ns.strip())


def record_seeded_ids(project_id: str, records: list[dict]) -> dict:
    """R254.SEED — harvest ids from a SEEDED create-response and persist them
    tagged `source="seeded"`.

    Kept as its own entry point (rather than a flag on extract_real_ids) so the
    provenance tag cannot be lost by an accidental default, and so the
    authorization check has exactly one home.

    Returns the persisted slots ({} when not authorized).
    """
    ok, reason = seeding_enabled()
    if not ok:
        log.debug("R254.SEED: seeding not authorized (%s)", reason)
        return {}
    ids = extract_real_ids(records)
    for slot in ids.values():
        slot["source"] = "seeded"
        slot["namespace"] = reason
    if ids:
        log.warning(
            "R254.SEED: ARTA CREATED %d test-data id slot(s) in the SUT under "
            "namespace '%s' for project %s — this WRITES to the SUT (R154 "
            "opt-in). Entities: %s. Unset ARTA_R154_ALLOW_DESTRUCTIVE_TESTS to "
            "restore default-deny.",
            len(ids), reason, project_id,
            ", ".join(sorted({s.get("entity", "") for s in ids.values()} - {""})) or "none",
        )
        persist_real_ids(project_id, ids)
    return ids


def seeded_id_values(project_id: str) -> set[str]:
    """Ids ARTA CREATED, as opposed to ids the SUT already had.

    R258 must never treat a failure on a seeded id as genuine SUT signal: if
    ARTA's own fixture is wrong or was cleaned up, that is ARTA's defect, and
    filing it against the SUT is the exact misreporting this plan exists to
    stop.
    """
    out: set[str] = set()
    for slot in load_real_ids(project_id).values():
        if isinstance(slot, dict) and slot.get("source") == "seeded":
            out.update(str(v) for v in (slot.get("values") or []))
    return out
