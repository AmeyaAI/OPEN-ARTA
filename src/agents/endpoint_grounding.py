"""R160 — endpoint grounding: clean the discovered-endpoint surface + map a
generated request path onto the real captured path shape.

Generated Newman/contract paths drift from the SUT's real API surface (wrong
segment order, an invented `/api/` prefix, `{account_id}` where the SUT uses
`{organization_id}`, etc.) → 404/500 for PATH reasons, independent of auth.
The fix is to GROUND paths in what the SUT actually served (captured traffic in
`.arta/discovered_endpoints/<pid>.json`).

But that captured surface is itself noisy — the harvester emitted double slashes
(`/svc//<id>//<id>`), URL-encoded braces (`/extraction/%7Bid_id%7D`), and
malformed template vars (`{{subscriber,Id}}`). So step 1 is to CLEAN it; step 2
is value-match concrete id segments to `{var}` (B1-style); step 3 is a structural
matcher that maps a generated path to the closest real template.

All deterministic — no LLM. SUT-agnostic: ids come from the caller's known-id map
(JWT claims + env vars). Used by gen-time grounding + dispatch-time repair.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("arta.endpoint_grounding")

# A path segment that looks like a concrete id (UUID-ish, long hex, or a known
# slug) — candidate for templating to {var}.
_UUIDISH = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{12,}$")
_LONGHEX = re.compile(r"^[0-9a-fA-F]{16,}$")
# Malformed template markers the harvester sometimes emitted — drop such paths.
_MALFORMED = re.compile(r"\{\{|\}\}|,|\$\{|%7[Bb].*%7[Dd]|<|>")


def _looks_like_id(seg: str) -> bool:
    return bool(_UUIDISH.match(seg) or _LONGHEX.match(seg))


# R160.G — synthesize a value for a REQUIRED query param that has no captured
# sample (OpenAPI declares the name but no example/default). Resolves the
# FastAPI 422 "field required" for free-form params (Class A). Chained/stateful
# params like correlation_id/session_id need real prior-call values (Class B —
# CallChain dataflow) — synthesis there at least surfaces a clearer business
# error than an empty value. Name-heuristic first, then type.
_SYNTH_UUID_HINTS = ("correlation", "session", "request_id", "trace")


def synth_query_value(name: str, ptype: str | None = None) -> str:
    """Deterministic synthetic value for a required query param by name+type."""
    n = (name or "").lower()
    if any(h in n for h in _SYNTH_UUID_HINTS) or n == "id" or n.endswith("_id") or n.endswith("id"):
        return "00000000-0000-4000-8000-000000000000"  # stable synthetic UUID
    if any(h in n for h in ("message", "query", "prompt", "text", "question")):
        return "arta test query"
    if "email" in n:
        return "arta-test@example.com"
    if any(h in n for h in ("date", "time")):
        return "2024-01-01"
    if "name" in n or "slug" in n or "type" in n:
        return "arta-test"
    if ptype in ("integer", "number"):
        return "1"
    if ptype == "boolean":
        return "true"
    if ptype == "array":
        return ""
    return "arta-test"


# Self-pollution: paths ARTA's OWN discovery probe injected (its hardcoded
# API_PROBES under /api/v1/*, synthetic test data) + static assets + non-API
# noise. Grounding must NOT treat these as the SUT's real surface — they are
# either ARTA's guesses (often 404) or non-endpoint traffic. SUT-agnostic: the
# /api/v1 + arta-synthetic patterns are ARTA's KNOWN footprint, the rest is
# generic web noise.
_STATIC_EXT = (".js", ".css", ".map", ".woff", ".woff2", ".ttf", ".png", ".jpg",
               ".jpeg", ".svg", ".ico", ".gif", ".webp", ".mp4", ".json")
_POLLUTION = re.compile(
    r"(^/api/v1/)|(arta[-_]synthetic)|(^/static/)|(^/assets/)|(/socket)|"
    r"(config\.js)|(^/images?/)|(^/icon)|(^/files?/)|(/ajax/)|(google\.firestore)",
    re.I,
)


def _is_pollution(path: str) -> bool:
    """True when `path` is ARTA self-injected probe traffic or static/non-API
    noise — excluded from the real-surface grounding set."""
    if not path:
        return True
    low = path.lower().split("?")[0]
    if low.endswith(_STATIC_EXT):
        return True
    return bool(_POLLUTION.search(low))


def clean_path(raw: str) -> str | None:
    """Normalize one captured path. Returns the cleaned path, or None when it's
    irreparably malformed (so the caller drops it from the grounding set)."""
    if not raw or not isinstance(raw, str):
        return None
    p = unquote(raw).split("?")[0].split("#")[0]
    p = re.sub(r"/{2,}", "/", p)
    if not p.startswith("/"):
        return None
    # after decoding, reject anything still carrying malformed template syntax
    if _MALFORMED.search(p):
        return None
    return p.rstrip("/") or "/"


def template_path(path: str, known_ids: dict[str, str] | None) -> str:
    """Replace concrete id segments with `{var}` placeholders. Value-matches
    against `known_ids` (value→var, B1-style) first; unmatched id-looking
    segments become a generic `{id}`. Non-id segments are kept verbatim.

    R217 — cm account slots are templated POSITIONALLY (not by value). The
    cm grammar is `/{account_id}/api/collection/{account_id}/...` (account_id in
    the leading slot AND the slot right after `collection`). Value-matching alone
    fails when the CAPTURED path carries a STALE account (an old session's
    `0aee6bd7…`) while the live session uses a different one (`955934e1…`): the stale
    segment doesn't match `known_ids[account_id]` → becomes a generic `{id}` →
    unresolved at dispatch. Positional templating maps both account slots to
    `{account_id}` regardless of the captured value, so dispatch fills them with
    the LIVE account (see auth_chain's account_id→root_account_id override).
    Mirrors the api_discovery N1 cm resolver. Killswitch ARTA_R217_CM_POS_TEMPLATE_DISABLE=1.
    """
    known_ids = known_ids or {}
    val_to_var = {v: k for k, v in known_ids.items() if isinstance(v, str) and v}
    segs = path.split("/")
    # R217 — detect cm-family path + the account-slot indices (positional).
    _cm_pos = os.environ.get("ARTA_R217_CM_POS_TEMPLATE_DISABLE") != "1"
    _account_slots: set[int] = set()
    if _cm_pos and "api" in segs and "collection" in segs:
        nonempty = [i for i, s in enumerate(segs) if s]
        # leading id-shaped segment → account_id
        if nonempty and _looks_like_id(segs[nonempty[0]]):
            _account_slots.add(nonempty[0])
        # the slot immediately after `collection` → account_id when it is the
        # SAME value as the leading account (the cm account-in-both-slots shape;
        # a DIFFERENT value there is a genuine collection_id — leave it).
        for i, s in enumerate(segs):
            if s == "collection" and i + 1 < len(segs) and _looks_like_id(segs[i + 1]):
                if nonempty and segs[i + 1] == segs[nonempty[0]]:
                    _account_slots.add(i + 1)
    out = []
    for i, seg in enumerate(segs):
        if not seg:
            out.append(seg)
            continue
        if i in _account_slots:
            out.append("{account_id}")
        elif seg in val_to_var:
            out.append("{%s}" % val_to_var[seg])
        elif _looks_like_id(seg):
            out.append("{id}")
        else:
            out.append(seg)
    return "/".join(out)


def build_grounding_index(
    endpoints: list[dict],
    known_ids: dict[str, str] | None = None,
    *,
    exclude_pollution: bool = True,
) -> dict[str, list[dict]]:
    """Clean + template the discovered endpoints into a family→[templates] index.

    Family = first non-empty path segment (`billing`, `analytics`, `extraction`, …).
    Each entry: {"method","template","segs","raw"}. Deduplicated. Malformed
    captures are dropped; when `exclude_pollution` (default), ARTA's own probe
    guesses (/api/v1/*), synthetic injections, and static/non-API noise are
    also dropped — leaving the SUT's REAL served surface. Counts under family
    '' as {"_dropped","_polluted"}.
    """
    index: dict[str, list[dict]] = {}
    seen: dict[tuple, dict] = {}   # R217 — key→entry (was a set) to merge query params
    dropped = 0
    polluted = 0
    for e in endpoints or []:
        if not isinstance(e, dict):
            continue
        method = (e.get("method") or "GET").upper()
        raw_path = e.get("path") or e.get("url") or ""
        if exclude_pollution and _is_pollution(raw_path):
            polluted += 1
            continue
        cleaned = clean_path(raw_path)
        if cleaned is None:
            dropped += 1
            continue
        if exclude_pollution and _is_pollution(cleaned):
            polluted += 1
            continue
        tmpl = template_path(cleaned, known_ids)
        segs = [s for s in tmpl.split("/") if s]
        if not segs:
            continue
        fam = segs[0]
        key = (method, tmpl)
        _qps = [q for q in (e.get("query_params") or [])
                if isinstance(q, dict) and q.get("name")]
        if key in seen:
            # R217 — same template seen before: MERGE this capture's required
            # query params into the existing entry (union by name) rather than
            # dropping them. Several captures of the same logical endpoint
            # templatize identically but split params (one capture carried
            # `filters/page_size/...`, another none) — keeping only the FIRST
            # dropped the cm LIST contract params → SUT 500. Mirror the harvest's
            # union-by-name merge so the grounded request carries them.
            _existing = seen[key]
            if _qps and isinstance(_existing, dict):
                _have = {q.get("name") for q in _existing.get("query_params") or []}
                for q in _qps:
                    if q.get("name") not in _have:
                        _existing.setdefault("query_params", []).append(q)
                        _have.add(q.get("name"))
            continue
        entry = {"method": method, "template": tmpl, "segs": segs, "raw": cleaned,
                 # R160.B — carry the required query-param names the SUT served with
                 "query_params": _qps}
        seen[key] = entry
        index.setdefault(fam, []).append(entry)
    index.setdefault("", []).append({"_dropped": dropped, "_polluted": polluted})
    return index


def build_grounded_newman_collection(
    name: str,
    index: dict[str, list[dict]],
    *,
    known_ids: dict[str, str] | None = None,
    methods: tuple[str, ...] = ("GET",),
    max_items: int = 100,
    only_resolvable: bool = True,
) -> dict:
    """R160 — deterministically emit a Newman collection GROUNDED in the clean
    real endpoint surface (the output of `build_grounding_index`).

    Each real templated endpoint becomes a contract-test item:
      GET {{base_url}}<real-path>  (with `{var}` → `{{var}}` for dispatch
      resolution), header `Authorization: Bearer {{auth_token}}` (the R159
      dispatch pre-request rewrites per-family host+Bearer), plus a status-class
      test asserting the response is NOT 5xx (path+auth correct → server-side
      handling reached).

    READ-ONLY by default (`methods=("GET",)`) → R154 non-mutation safe. When
    `only_resolvable`, keep only endpoints whose every `{var}` is in
    `known_ids` (so they fully resolve at dispatch) — the cleanest contract set.
    Paths are REAL (came from captured traffic), so they can't 404 for the
    invented-shape reason the LLM-generated collections do.
    """
    known = set((known_ids or {}).keys())
    items: list[dict] = []
    seen: set[str] = set()
    for fam, entries in index.items():
        if not fam:
            continue
        for e in entries:
            tmpl = e.get("template")
            if not tmpl or e.get("method") not in methods:
                continue
            vars_in = re.findall(r"\{(\w+)\}", tmpl)
            if only_resolvable and any(v not in known for v in vars_in):
                continue
            if tmpl in seen:
                continue
            seen.add(tmpl)
            nm_url = re.sub(r"\{(\w+)\}", r"{{\1}}", tmpl)
            # R160.B — include the REQUIRED query params the SUT served with, so
            # the contract request isn't rejected with 400 "<param> is required".
            # id-ish sample values are templated to {{var}}; others kept verbatim.
            qps = e.get("query_params") or []
            query_objs = []
            _val2var = {v: k for k, v in (known_ids or {}).items() if isinstance(v, str) and v}
            for q in qps:
                nm = q.get("name")
                if not nm:
                    continue
                val = q.get("value") or ""
                # R217 — the HAR redactor can replace a query-param VALUE with a
                # placeholder (`<<REDACTED_HEADER_VALUE>>`, `***`, `REDACTED`);
                # sending that literal string 500s the SUT (live: cm LIST reads
                # send `last_evaluated_key=<<REDACTED_HEADER_VALUE>>` → 500 while
                # the browser sends `last_evaluated_key=null`). Treat a redacted
                # value as ABSENT so the name-heuristic below supplies a safe one
                # (pagination cursors/filters → "null"). Killswitch
                # ARTA_R217_REDACTED_QP_FIX_DISABLE=1.
                if (isinstance(val, str)
                        and os.environ.get("ARTA_R217_REDACTED_QP_FIX_DISABLE") != "1"
                        and ("redacted" in val.lower() or val in ("***", "<<REDACTED>>"))):
                    nlow = nm.lower()
                    # pagination cursors + filters default to the SUT's own "null"
                    val = "null" if any(h in nlow for h in (
                        "evaluated_key", "cursor", "filter", "token", "next")) else ""
                if isinstance(val, str) and val and val in _val2var:
                    val = "{{%s}}" % _val2var[val]          # captured id → {{var}}
                elif not val:
                    # R160.G — no captured value: template known-id names, else
                    # synthesize by name/type so required params aren't empty (422).
                    if nm in (known_ids or {}):
                        val = "{{%s}}" % nm
                    else:
                        val = synth_query_value(nm, q.get("type"))
                query_objs.append({"key": nm, "value": val})
            qstr = "&".join(f"{q['key']}={q['value']}" for q in query_objs)
            raw = "{{base_url}}" + nm_url + (("?" + qstr) if qstr else "")
            url_obj = {"raw": raw, "host": ["{{base_url}}"],
                       "path": [s for s in nm_url.split("/") if s]}
            if query_objs:
                url_obj["query"] = query_objs
            items.append({
                "name": f"{e['method']} {tmpl}",
                "request": {
                    "method": e["method"],
                    "header": [{"key": "Authorization", "value": "Bearer {{auth_token}}"}],
                    "url": url_obj,
                },
                "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": [
                    "pm.test('grounded ' + pm.response.code + ' (not 5xx)', function () {",
                    "  pm.expect(pm.response.code).to.be.below(500);",
                    "});",
                ]}}],
            })
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    return {
        "info": {"name": name, "_arta_grounded": True,
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": items,
    }


def reground_collection_paths(
    collection: dict,
    index: dict[str, list[dict]],
    known_ids: dict[str, str] | None = None,
) -> tuple[dict, int, int]:
    """R174 — rewrite each Newman request PATH to the nearest REAL captured
    template (via ground_path). LLM/OpenAPI contract paths frequently use a
    shape the SUT doesn't serve (missing a leading family prefix like /svc,
    or a trailing resource segment like /collection-modules) → 404/500 that look
    like SUT bugs. Regrounding maps them onto the real served shape. Only the
    PATH is rewritten (segments + raw path portion); host stays so the R159
    pre-request still routes per family; query is preserved. Items whose path
    has no confident real match are left unchanged (may be a valid uncaptured
    endpoint). Returns (collection, n_regrounded, n_unmatched)."""
    n_rg = 0
    n_un = 0

    def _path_str(url) -> str:
        if isinstance(url, dict):
            return "/" + "/".join(str(s) for s in (url.get("path") or []))
        return url if isinstance(url, str) else ""

    def _apply(it: dict) -> None:
        nonlocal n_rg, n_un
        req = it.get("request") or {}
        if not isinstance(req, dict):
            return
        url = req.get("url")
        cur = _path_str(url)
        if not cur or cur == "/":
            return
        # Newman paths use Postman `{{var}}` — clean_path() rejects `{{` as
        # malformed, so normalize to the real value (when known) else `{var}`
        # single-brace before grounding, so template_path can value-match.
        _kn = known_ids or {}
        cur_norm = re.sub(
            r"\{\{(\w+)\}\}",
            lambda m: _kn.get(m.group(1)) or ("{" + m.group(1) + "}"),
            cur,
        )
        g = ground_path(cur_norm, index, method=req.get("method") or "GET", known_ids=known_ids)
        if not g:
            n_un += 1
            return
        # R174 SAFETY (conservative) — only reground when the real template is a
        # genuine COMPLETION of the generated path (real ⊇ generated literals,
        # /collection-modules), NEVER a same-length SIBLING swap. The fuzzy
        # "nearest literals" match could remap a CORRECT path to a wrong sibling
        # endpoint and break a passing request (run-841cdb: PASS 312→224). A
        # completion is safe; a swap is not. Require every generated literal to
        # be present in the real template's literals.
        gtmpl_norm = template_path(clean_path(cur_norm) or cur_norm, known_ids)
        g_lits = set(_literal_segs([s for s in gtmpl_norm.split("/") if s]))
        real_lits = set(_literal_segs([s for s in g["template"].split("/") if s]))
        if g_lits and not g_lits.issubset(real_lits):
            n_un += 1
            return  # would be a sibling swap, not a safe completion — leave as-is
        new_segs = [s for s in re.sub(r"\{(\w+)\}", r"{{\1}}", g["template"]).split("/") if s]
        if [s for s in cur.split("/") if s] == new_segs:
            return  # already grounded — no change
        if isinstance(url, dict):
            old_q = url.get("query")
            qstr = ""
            if isinstance(old_q, list) and old_q:
                qstr = "?" + "&".join(f"{x.get('key')}={x.get('value')}" for x in old_q)
            url["path"] = new_segs
            url["raw"] = "{{base_url}}/" + "/".join(new_segs) + qstr
        else:
            req["url"] = {"raw": "{{base_url}}/" + "/".join(new_segs),
                          "host": ["{{base_url}}"], "path": new_segs}
        it.setdefault("_arta_meta", {})["r174_regrounded_from"] = cur
        n_rg += 1

    def _walk(items):
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("item"), list):
                _walk(it["item"])
            elif it.get("request"):
                _apply(it)

    _walk(collection.get("item") or [])
    return collection, n_rg, n_un


def _literal_segs(segs: list[str]) -> list[str]:
    return [s for s in segs if not (s.startswith("{") and s.endswith("}"))]


def _is_tmpl_seg(s: str) -> bool:
    return s.startswith("{") and s.endswith("}")


def _suffix_aligns(real_segs: list[str], gen_segs: list[str]) -> bool:
    """R174 — True when `real_segs` ENDS WITH `gen_segs` under the alignment
    rule: a `{id}` template segment matches any `{id}` template segment, but a
    literal must match the same literal exactly. (So real = <prefix> + gen.)"""
    if len(real_segs) < len(gen_segs) or not gen_segs:
        return False
    tail = real_segs[len(real_segs) - len(gen_segs):]
    for r, g in zip(tail, gen_segs):
        rt, gt = _is_tmpl_seg(r), _is_tmpl_seg(g)
        if rt and gt:
            continue
        if rt != gt or r != g:
            return False
    return True


def ground_path(
    gen_path: str,
    index: dict[str, list[dict]],
    *,
    method: str | None = None,
    known_ids: dict[str, str] | None = None,
) -> dict | None:
    """Map a generated path onto the closest REAL captured template.

    Strategy (deterministic): template the generated path the same way, then
    among candidates of the same family (and method when given) pick the one
    maximizing shared LITERAL (non-id) segments, tie-broken by closest segment
    count. Returns {"template","raw","method","score"} or None when no
    same-family candidate exists (caller leaves the path unchanged).
    """
    cleaned = clean_path(gen_path)
    if cleaned is None:
        return None
    gtmpl = template_path(cleaned, known_ids)
    gsegs = [s for s in gtmpl.split("/") if s]
    if not gsegs:
        return None
    fam = gsegs[0]
    # tolerate an invented leading `/api` prefix: try fam and the next segment
    candidates = index.get(fam) or (index.get(gsegs[1]) if len(gsegs) > 1 else None) or []
    candidates = [c for c in candidates if "template" in c]
    if method:
        m = [c for c in candidates if c["method"] == method.upper()]
        candidates = m or candidates
    # R229.A — when the same-family lookup finds NOTHING, do NOT return early: the
    # suffix-match fallback below is designed for exactly that case (the LLM dropped
    # the real leading family prefix, so `fam` = a mid-path segment that no captured
    # path starts with — e.g. gen `/pipeline/event/get` vs real
    # `/entityextractionagent/.../function/pipeline/event/get`). The old
    # `if not candidates: return None` short-circuited the fallback into dead code,
    # leaving these truncated paths as `unknown_endpoint` (24 grounding-blocked newman
    # items in run-964ddf). Only run the same-family scoring when candidates exist.
    if candidates:
        glit = set(_literal_segs(gsegs))
        best, best_score = None, -1.0
        for c in candidates:
            clit = set(_literal_segs(c["segs"]))
            if not (glit or clit):
                shared = 0.0
            else:
                shared = len(glit & clit)
            # prefer more shared literals; tie-break by closeness of length
            score = shared - 0.1 * abs(len(c["segs"]) - len(gsegs))
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= 0:
            return {"template": best["template"], "raw": best["raw"],
                    "method": best["method"], "score": best_score}

    # R174 — prefix/suffix-match fallback. The LLM/OpenAPI contract paths often
    # scoring above finds nothing. Here we scan ALL families for a real template
    # whose segments END WITH the generated segments (id-templates wildcard-match)
    # and whose literal segments are a SUPERSET of the generated literals — i.e.
    # real == <missing-prefix> + generated. Safe: requires full suffix alignment
    # + every generated literal present + ≥1 shared literal to anchor.
    g_lit = set(_literal_segs(gsegs))
    if not g_lit:
        return None  # no literal anchor → too risky to remap
    pbest, pbest_score = None, -1.0
    for fam_c, clist in index.items():
        if not fam_c:
            continue
        for c in clist:
            if "segs" not in c:
                continue
            if method and c.get("method") != method.upper():
                continue
            rs = c["segs"]
            if len(rs) < len(gsegs):
                continue
            if not _suffix_aligns(rs, gsegs):
                continue
            clit = set(_literal_segs(rs))
            if not g_lit.issubset(clit):
                continue
            extra = len(rs) - len(gsegs)          # length of the missing prefix
            score = len(g_lit) - 0.1 * extra      # most shared literals, least extra
            if score > pbest_score:
                pbest, pbest_score = c, score
    if pbest is not None and pbest_score >= 1:
        return {"template": pbest["template"], "raw": pbest["raw"],
                "method": pbest["method"], "score": pbest_score,
                "_r174_prefix_match": True}
    return None


# ── R160.E — production grounded-collection generation ────────────────────────

def collect_known_id_values(project: dict, env_block: dict | None = None) -> dict:
    """Gather concrete id VALUES (org/account/subscriber/subscription/schema/
    user) ARTA can resolve, for templating + the dispatch vars. Sources: the
    env-block `variables` (operator-declared) + the session cookie's JWT claims.
    Skips REPLACE_ME / empty. SUT-agnostic (only keeps id-shaped names)."""
    out: dict[str, str] = {}
    eb = env_block or {}
    for k, v in (eb.get("variables") or {}).items():
        if isinstance(v, str) and v and v != "REPLACE_ME" and (k.endswith("_id") or k in ("schema_id",)):
            out[k] = v
    try:
        from .auth_refresher import (
            _find_storage_state_path, _read_storage_state, _decode_jwt_payload,
        )
        sp = _find_storage_state_path(eb.get("name") if isinstance(eb, dict) else None)
        storage = _read_storage_state(sp) if sp else None
        if storage:
            for c in storage.get("cookies") or []:
                if "token" in (c.get("name") or "").lower():
                    claims = _decode_jwt_payload(c.get("value") or "") or {}
                    for k, v in claims.items():
                        if isinstance(v, str) and v and (k.endswith("_id") or k == "schema_id"):
                            out.setdefault(k, v)
                    # organizations[0] → organization_id (B1 canonical)
                    orgs = claims.get("organizations")
                    if isinstance(orgs, list) and orgs and isinstance(orgs[0], str):
                        out.setdefault("organization_id", orgs[0])
                    break
    except Exception as exc:  # pragma: no cover
        log.debug("R160.E: claim id-value gather skipped: %s", exc)
    return out


def write_grounded_collection(
    out_dir: "str | Path",
    prefix: str,
    endpoints: list[dict],
    known_ids: dict[str, str],
    *,
    name: str = "ARTA Grounded Contract Tests",
    methods: tuple[str, ...] = ("GET",),
    max_items: int = 120,
) -> "Path | None":
    """R160.E — build a grounded contract collection from the discovered surface
    and write it to `<out_dir>/<prefix>grounded_api.json` so the Newman
    dispatcher (which filters by `<prefix>`) picks it up. READ-ONLY (GET) →
    R154-safe + additive (sits ALONGSIDE existing LLM collections, no replace).
    Returns the path, or None when there's nothing fully-resolvable to ground.
    """
    idx = build_grounding_index(endpoints, known_ids)
    coll = build_grounded_newman_collection(name, idx, known_ids=known_ids,
                                            methods=methods, max_items=max_items)
    if not coll.get("item"):
        return None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pfx = prefix if prefix.endswith("_") else (prefix + "_") if prefix else ""
    path = out / f"{pfx}grounded_api.json"
    path.write_text(json.dumps(coll, indent=2))
    log.info("R160.E: wrote grounded contract collection %s (%d items)", path, len(coll["item"]))
    return path
