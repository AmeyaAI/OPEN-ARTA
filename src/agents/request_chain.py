"""R175 — runtime request-chaining for Newman collections.

Deep SUT endpoints often need real resource-instance ids (collection_id,
fieldset_id, file_id, …) that are NOT in the session token and NOT in the
captured surface (only 6/352 endpoints captured a response_body_shape, none
with id fields) — so they can't be mapped statically. Instead we chain at
RUNTIME, inside a single Newman run, with three deterministic, SUT-agnostic
steps:

  1. Inject a COLLECTION-LEVEL test script that runs after every request and
     recursively harvests any id-shaped field (`*_id` / `id`) from a 2xx JSON
     response into `pm.collectionVariables`. So a list/index response that
     contains `[{collection_id: ...}]` populates `{{collection_id}}`.
  2. ORDER items by ascending path-variable count so the broad list/index
     endpoints (which return the ids) run BEFORE the deep detail endpoints
     (which consume them) — Newman resolves `{{var}}` from collection vars set
     by earlier items.
  3. SEED the consumed resource-id vars as empty collection variables so the
     dispatch-time unresolved-var filter (R29.3a/R170) doesn't drop the items
     before they get a chance to be chained; the runtime harvest overwrites
     them with real values when a provider response carries them.

If no provider response carries a given id, the var stays empty and the request
honestly 404s (couldn't chain) rather than fabricating a fake id. Opt-in:
ARTA_R175_CHAIN_ENABLE=1.
"""
from __future__ import annotations

import re

# id-shaped variable names this chaining harvests + consumes. SUT-agnostic:
# resource ids so their items aren't pre-filtered.
_ID_VAR_RE = re.compile(r"(?:_id|_uuid)$|^id$", re.IGNORECASE)

# The collection-level harvest script (runs after EVERY request in the run).
_HARVEST_SCRIPT = [
    "// R175 — harvest id-shaped fields from this response into collection vars",
    "try {",
    "  var __j = pm.response.json();",
    "  var __harvest = function (o, depth) {",
    "    if (!o || typeof o !== 'object' || depth > 6) { return; }",
    "    if (Array.isArray(o)) { for (var i = 0; i < o.length && i < 50; i++) { __harvest(o[i], depth + 1); } return; }",
    "    for (var k in o) {",
    "      if (!Object.prototype.hasOwnProperty.call(o, k)) { continue; }",
    "      var v = o[k];",
    "      if ((/(_id|_uuid)$/i.test(k) || /^id$/i.test(k)) && (typeof v === 'string' || typeof v === 'number') && String(v).length) {",
    "        pm.collectionVariables.set(k, String(v));",
    "      } else if (v && typeof v === 'object') { __harvest(v, depth + 1); }",
    "    }",
    "  };",
    "  if (pm.response.code >= 200 && pm.response.code < 300) { __harvest(__j, 0); }",
    "} catch (e) { /* non-JSON or empty body — nothing to harvest */ }",
]


def is_id_var(name: str) -> bool:
    return bool(name) and bool(_ID_VAR_RE.search(name))


def _path_var_count(item: dict) -> int:
    """Number of `{{var}}` path segments — a proxy for endpoint depth (list
    endpoints have fewer; deep detail endpoints have more)."""
    req = item.get("request") or {}
    url = req.get("url") if isinstance(req, dict) else None
    segs = []
    if isinstance(url, dict):
        segs = url.get("path") or []
    elif isinstance(url, str):
        segs = url.split("/")
    return sum(1 for s in segs if isinstance(s, str) and "{{" in s)


def _consumed_id_vars(collection: dict) -> set[str]:
    """All id-shaped `{{var}}` referenced anywhere in the collection's URLs."""
    found: set[str] = set()
    blob = []

    def _walk(items):
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("item"), list):
                _walk(it["item"])
            elif it.get("request"):
                u = (it["request"] or {}).get("url")
                if isinstance(u, dict):
                    blob.append(u.get("raw") or "")
                    blob.extend(str(s) for s in (u.get("path") or []))
                elif isinstance(u, str):
                    blob.append(u)
    _walk(collection.get("item") or [])
    for m in re.findall(r"\{\{(\w+)\}\}", " ".join(blob)):
        if is_id_var(m):
            found.add(m)
    return found


def apply_request_chaining(collection: dict, already_resolved: set[str] | None = None) -> tuple[dict, dict]:
    """R175 — make `collection` chain resource ids at runtime. Returns
    (collection, info) where info = {chained_vars, reordered, seeded}.

    `already_resolved` = vars already injected as real values (session ids from
    R167, captured values from R169) — those are NOT seeded/relied-on for
    chaining (they're already correct).
    """
    resolved = already_resolved or set()
    consumed = {v for v in _consumed_id_vars(collection) if v not in resolved}

    # 1. Inject the collection-level harvest test event (idempotent).
    events = collection.setdefault("event", [])
    if not any((e.get("listen") == "test" and "R175" in " ".join(
            (e.get("script") or {}).get("exec") or [])) for e in events if isinstance(e, dict)):
        events.append({"listen": "test", "script": {"type": "text/javascript", "exec": list(_HARVEST_SCRIPT)}})

    # 2. Order items by ascending path-var depth (providers before consumers).
    #    Only reorders FLAT item lists (folders left in place, recursed).
    def _reorder(items):
        flat = [it for it in items if isinstance(it, dict) and it.get("request")]
        folders = [it for it in items if isinstance(it, dict) and isinstance(it.get("item"), list)]
        for f in folders:
            f["item"] = _reorder(f["item"])
        flat_sorted = sorted(flat, key=_path_var_count)
        # keep folders after flats (folders are usually grouped, order-agnostic)
        return flat_sorted + folders
    collection["item"] = _reorder(collection.get("item") or [])

    # 3. Seed consumed id vars as empty collection variables so the unresolved-
    #    var filter doesn't drop the items before chaining can populate them.
    cvars = collection.setdefault("variable", [])
    existing = {v.get("key") for v in cvars if isinstance(v, dict)}
    seeded = []
    for v in sorted(consumed):
        if v not in existing:
            cvars.append({"key": v, "value": ""})
            seeded.append(v)

    return collection, {"chained_vars": sorted(consumed), "reordered": True, "seeded": seeded}
