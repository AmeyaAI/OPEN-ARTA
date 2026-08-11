"""R297 (Option B) — generic request-body DTO extraction from SUT source.

The capability unlock behind the read-POST 500s. `build_request_bodies` (R211)
synthesizes bodies from OpenAPI / captured shapes / probe-captured bodies — but
the blocked families (.NET-based services) publish NO reachable OpenAPI, and
the probe never triggers their POSTs, so all three sources are empty. The body
shape DOES exist: the AC names the DTO (`POST /Reefer/... (CommandCenterRequest)`)
and the class lives in the SUT's own source, reachable via the agent-owned GitHub
MCP (R104.B).

This module is SUT-AGNOSTIC by construction (the standing genericity directive):
a per-language DTO parser (C#/.NET, Java, TypeScript), no SUT literals. It emits
a `{ "METHOD path": example_body }` map that feeds `build_request_bodies` as its
4th source and flows through R295's gen seam with no new injection path. When a
DTO can't be resolved/parsed, it yields nothing → R296 reports the gap truthfully.

This file holds the PURE, offline-testable core (resolve + parse). The live MCP
fetch/orchestration is thin and composes github_context's existing search_code /
get_file.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("arta.dto")

# A DTO type named in an AC/requirement, e.g. "POST /x (CommandCenterRequest)" or
# "with a CommandCenterRequest body" or "body: SaveAssetDto". PascalCase ending
# in a request/DTO-ish suffix, or explicitly introduced by `(...)`/`body`.
_DTO_TYPE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:Request|Req|Dto|DTO|Model|Payload|Body|Command|Query|Filter|Input))\b"
)
_DTO_PAREN_RE = re.compile(r"\(\s*([A-Z][A-Za-z0-9]{2,})\s*\)")


def resolve_dto_type_name(text: str) -> str | None:
    """Best-effort DTO type name from requirement/AC prose. Prefers a
    `(TypeName)` parenthetical (how some SUTs' ACs name it), else a PascalCase
    token with a request/DTO-ish suffix. Returns None when nothing looks like a
    DTO — never guesses a bare word."""
    if not text:
        return None
    m = _DTO_PAREN_RE.search(text)
    if m:
        return m.group(1)
    m = _DTO_TYPE_RE.search(text)
    return m.group(1) if m else None


# ── per-language field extraction ────────────────────────────────────────────

# C#:  public string AccountId { get; set; }   |   public List<string> Ids {get;set;}
_CS_FIELD_RE = re.compile(
    r"public\s+(?:virtual\s+|required\s+)?"
    r"([A-Za-z0-9_<>,\?\[\]\.]+)\s+"          # type
    r"([A-Za-z_][A-Za-z0-9_]*)\s*"            # name
    r"\{\s*get\s*;",
)
# Java: private String accountId;  |  private List<String> ids;
_JAVA_FIELD_RE = re.compile(
    r"(?:private|protected|public)\s+(?:final\s+)?"
    r"([A-Za-z0-9_<>,\?\[\]\.]+)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*[;=]",
)
# TS: accountId: string;  |  ids: string[];  |  count?: number
_TS_FIELD_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\??\s*:\s*"
    r"([A-Za-z0-9_<>\[\]\.\| ]+?)\s*[;,]?\s*$",
    re.MULTILINE,
)


def _class_body(source: str, type_name: str) -> str | None:
    """Slice the `{...}` body of `class/interface <type_name>` (brace-balanced)."""
    m = re.search(
        r"\b(?:class|interface|record|struct)\s+" + re.escape(type_name)
        + r"\b[^{]*\{",
        source,
    )
    if not m:
        return None
    i = m.end() - 1  # at the opening brace
    depth = 0
    for j in range(i, len(source)):
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[i + 1:j]
    return source[i + 1:]   # unbalanced — take the rest


def _example_for_type(t: str, field: str) -> object:
    """Map a declared type to a minimal valid example value. id-shaped fields get
    a placeholder the caller can overwrite with a real harvested id."""
    tl = t.strip().lower().rstrip("?")
    # collections
    if tl.endswith("[]") or tl.startswith(("list<", "ienumerable<", "icollection<",
                                           "array<", "set<", "array")):
        return []
    if tl.startswith(("dictionary<", "map<", "record<")) or tl == "object":
        return {}
    if tl in ("int", "integer", "long", "short", "byte", "int32", "int64", "number"):
        return 0
    if tl in ("double", "float", "decimal", "single"):
        return 0.0
    if tl in ("bool", "boolean"):
        return False
    if tl in ("datetime", "datetimeoffset", "date", "instant"):
        return "2026-01-01T00:00:00Z"
    if tl in ("guid", "uuid"):
        return "00000000-0000-0000-0000-000000000000"
    # string / unknown scalar → placeholder
    return "example"


def parse_dto_source(source: str, type_name: str) -> dict | None:
    """Parse a DTO class/interface `type_name` from `source` into an example
    request body. Auto-detects C# / Java / TypeScript by which field pattern
    matches inside the class body. Returns None when the type isn't found or no
    fields parse (so the caller falls back to the truthful gap, not a `{}`)."""
    body = _class_body(source, type_name)
    if body is None:
        return None
    out: dict = {}
    # C# property syntax is the most specific — try it first.
    for m in _CS_FIELD_RE.finditer(body):
        out[m.group(2)] = _example_for_type(m.group(1), m.group(2))
    if not out:
        for m in _JAVA_FIELD_RE.finditer(body):
            _t, _n = m.group(1), m.group(2)
            if _t in ("class", "interface", "return", "void"):   # not a field
                continue
            out[_n] = _example_for_type(_t, _n)
    if not out:
        for m in _TS_FIELD_RE.finditer(body):
            _n, _t = m.group(1), m.group(2)
            if _n in ("public", "private", "return", "function", "constructor"):
                continue
            out[_n] = _example_for_type(_t, _n)
    return out or None


# ── controller-source DTO resolution (R297.B) ────────────────────────────────
# When the requirement text doesn't name the DTO (the common case: captured
# endpoints carry no `declared_by` link), the AUTHORITATIVE body type is the SUT
# controller's own body parameter — Spring `@RequestBody T`, .NET `[FromBody] T`,
# NestJS `@Body() dto: T`. Derive the handler token from the path, find the method
# in source, read its body-param type. SUT-agnostic (framework-annotation driven).

_REQBODY_SPRING = re.compile(r"@RequestBody\s+(?:final\s+)?([A-Z][A-Za-z0-9_]+)")
_REQBODY_DOTNET = re.compile(r"\[FromBody\]\s*([A-Z][A-Za-z0-9_]+)")
_REQBODY_NEST = re.compile(r"@Body\s*\([^)]*\)\s*\w+\s*:\s*([A-Z][A-Za-z0-9_]+)")


def _handler_token_from_path(path: str) -> str | None:
    """Last non-parameter path segment — the likely handler-method name
    (`/Reefer/api/getDataForCommandCenter` → `getDataForCommandCenter`). Returns
    None when the tail is a `{param}`/numeric id (no method name to search)."""
    if not path:
        return None
    for seg in reversed([s for s in str(path).split("/") if s]):
        if seg.startswith("{") or seg.startswith(":") or seg.isdigit():
            continue
        # a real identifier-ish handler name (skip bare 'api'/'v1'/'v2' noise)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,}", seg) and seg.lower() not in (
                "api", "rest", "get", "post", "put", "v1", "v2", "v3"):
            return seg
    return None


def resolve_body_type_from_controller(source: str, handler_token: str) -> str | None:
    """Find `handler_token(...)` in controller source and extract its request-body
    parameter TYPE (Spring @RequestBody / .NET [FromBody] / NestJS @Body). Returns
    None when the method or a body annotation isn't found — never guesses."""
    if not source or not handler_token:
        return None
    for m in re.finditer(re.escape(handler_token) + r"\s*\(", source):
        start = m.end()
        depth, i = 1, start
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        params = source[start:i - 1]
        for _re in (_REQBODY_SPRING, _REQBODY_DOTNET, _REQBODY_NEST):
            mt = _re.search(params)
            if mt:
                return mt.group(1)
    return None


# ── async orchestration: resolve → search → fetch → parse ────────────────────

_BACKEND_EXT = (".java", ".cs", ".kt", ".py", ".go", ".rb")


def _ranked_source_paths(search_result: str, *, prefer_controller: bool = False,
                         type_name: str | None = None) -> list:
    """All (repo, path) hits from a github search result, best-first. Tolerant of
    the JSON-ish / text shapes. Prefers real source over tests; a file whose
    BASENAME is `<type_name>.<ext>` (the class's own definition file) ranks first
    when `type_name` is given; when `prefer_controller` (a handler-method lookup),
    ranks server-side controller files (backend extension, `controller`/`api` in
    the path) above frontend components — a bare handler token matches both the
    .NET/Java controller AND the React/Angular caller, and only the controller
    carries `@RequestBody`."""
    import json as _json
    if not search_result:
        return []
    hits: list = []
    try:
        parsed = _json.loads(search_result)
        items = (parsed.get("items") if isinstance(parsed, dict) else parsed) or []
        for it in items:
            repo = (((it.get("repository") or {}).get("full_name"))
                    or it.get("repo") or "")
            path = it.get("path") or it.get("name") or ""
            if path:
                hits.append((str(repo), str(path)))
    except Exception:
        for m in re.finditer(r"([\w.-]+/[\w.-]+)[^\n]*?([\w/.-]+\.(?:cs|java|ts))",
                             search_result):
            hits.append((m.group(1), m.group(2)))

    _base = (type_name or "").lower()

    def _key(rp):
        pl = rp[1].lower()
        is_test = "test" in pl or "spec" in pl
        # the type's own definition file (basename == <Type>.<ext>) always wins
        name_match = bool(_base) and pl.rsplit("/", 1)[-1].rsplit(".", 1)[0] == _base
        if prefer_controller:
            is_backend = pl.endswith(_BACKEND_EXT)
            is_ctrl = "controller" in pl or "/api/" in pl or "resource" in pl
            return (not name_match, is_test, not is_ctrl, not is_backend, len(rp[1]))
        return (not name_match, is_test, len(rp[1]))

    hits.sort(key=_key)
    # stable-dedup by (repo, path)
    seen, ordered = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def _top_source_path(search_result: str) -> tuple[str, str] | None:
    """Best single (repo, path) hit — used for DTO-class lookups (unambiguous)."""
    ranked = _ranked_source_paths(search_result)
    return ranked[0] if ranked else None


async def build_dto_request_bodies(
    project: dict,
    endpoints: list[dict],
    req_text_by_key: dict,
    *,
    search_fn=None,
    fetch_fn=None,
) -> dict:
    """R297 — for each bodyless POST/PUT/PATCH endpoint, resolve its DTO type
    from the declaring requirement's text, fetch the class from SUT source via
    the agent-owned GitHub MCP, and parse it into an example body.

    Returns `{ "METHOD path": example_body }`. `search_fn`/`fetch_fn` default to
    github_context.search_code / get_file and are INJECTABLE so the orchestration
    is unit-testable without live MCP. A DTO that can't be resolved/found/parsed
    is simply skipped (→ R296 reports the gap truthfully). SUT-agnostic: the type
    name and class source both come from the SUT's own requirements + repos.
    Killswitch handled by the caller (`ARTA_R297_DTO_EXTRACT_DISABLE`).
    """
    if search_fn is None or fetch_fn is None:
        from . import github_context as _gh
        search_fn = search_fn or _gh.search_code
        fetch_fn = fetch_fn or _gh.get_file

    import os as _os
    # GitHub code-search is rate-limited (~10 req/min authenticated); each endpoint
    # that needs a controller lookup costs 1 search. Cap per refresh and log what
    # was skipped (no-silent-truncation directive). Default 40 covers a typical
    # bodyless-POST set without starving the rate budget.
    _max = int(_os.environ.get("ARTA_R297_MAX_DTO_LOOKUPS") or "40")

    out: dict = {}
    _seen_dto: dict[str, dict | None] = {}       # type_name -> body (parse once)
    _seen_ctrl: dict[str, str | None] = {}       # handler_token -> DTO type name
    _lookups = 0
    _capped = 0

    async def _dto_body(dto: str) -> dict | None:
        """Search → fetch → parse a DTO class, cached by type name."""
        if dto in _seen_dto:
            return _seen_dto[dto]
        body = None
        try:
            _res = await search_fn(project, f"class {dto}")
            # try the top candidates — the type's own definition file first
            for _hit in _ranked_source_paths(_res or "", type_name=dto)[:3]:
                _src = await fetch_fn(project, _hit[0], _hit[1])
                body = parse_dto_source(_src or "", dto)
                if body:
                    break
        except Exception as _exc:
            log.debug("R297: DTO fetch/parse failed for %s: %s", dto, _exc)
            body = None
        _seen_dto[dto] = body
        return body

    for ep in endpoints or []:
        if not isinstance(ep, dict):
            continue
        m = str(ep.get("method") or "").upper()
        p = ep.get("path") or ""
        if m not in ("POST", "PUT", "PATCH") or not p:
            continue
        key = f"{m} {p}"

        # (1) requirement/AC prose names the DTO (e.g. `(CommandCenterRequest)`).
        dto = resolve_dto_type_name(str(req_text_by_key.get(key) or ""))

        # (2) R297.B — else derive it from the SUT controller's body parameter.
        if not dto:
            handler = _handler_token_from_path(p)
            if not handler:
                continue
            if handler in _seen_ctrl:
                dto = _seen_ctrl[handler]
            elif _lookups >= _max:
                _capped += 1
                continue
            else:
                _lookups += 1
                dto = None
                try:
                    _cres = await search_fn(project, handler)
                    # a bare handler matches BOTH the backend controller and the
                    # frontend caller — try the top controller-ranked candidates
                    # until one yields a @RequestBody/[FromBody] type.
                    for _chit in _ranked_source_paths(_cres or "", prefer_controller=True)[:3]:
                        _csrc = await fetch_fn(project, _chit[0], _chit[1])
                        dto = resolve_body_type_from_controller(_csrc or "", handler)
                        if dto:
                            break
                except Exception as _cexc:
                    log.debug("R297.B: controller lookup failed for %s: %s", handler, _cexc)
                    dto = None
                _seen_ctrl[handler] = dto
            if not dto:
                continue

        body = await _dto_body(dto)
        if body:
            out[key] = body

    if _capped:
        log.info("R297: %d bodyless endpoint(s) skipped controller lookup (cap "
                 "ARTA_R297_MAX_DTO_LOOKUPS=%d reached) — raise the cap to cover more",
                 _capped, _max)
    if out:
        log.info("R297: extracted %d request-body DTO(s) from SUT source", len(out))
    return out


# ── persist / load (R104.B Tier-1: pre-fetch MCP context, read sync at gen) ───

from pathlib import Path as _Path

_DTO_DIR = _Path(".arta/dto_request_bodies")


def persist_dto_request_bodies(project_id: str, bodies: dict) -> None:
    """Persist `{METHOD path: body}` so the SYNC gen path (build_request_bodies)
    can read the MCP-derived DTO bodies without being async — the R104.B Tier-1
    pattern (agent pre-fetches, sync path reads)."""
    if not project_id or not bodies:
        return
    try:
        import json as _json
        _DTO_DIR.mkdir(parents=True, exist_ok=True)
        (_DTO_DIR / f"{project_id}.json").write_text(_json.dumps(bodies, indent=2))
    except Exception as exc:
        log.debug("R297: DTO persist failed for %s: %s", project_id, exc)


def load_dto_request_bodies(project_id: str) -> dict:
    """Read persisted DTO bodies (build_request_bodies' 4th source). {} on miss."""
    if not project_id:
        return {}
    try:
        import json as _json
        p = _DTO_DIR / f"{project_id}.json"
        if p.is_file():
            d = _json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


async def refresh_dto_request_bodies(project: dict) -> dict:
    """R297 top-level entry: for every captured POST/PUT/PATCH endpoint WITHOUT an
    existing (OpenAPI/captured/Phase-F) body, resolve its DTO from the declaring
    requirement's text, extract it from SUT source, and persist. Returns the
    extracted map. Idempotent + best-effort; the sync gen/triage paths read the
    persisted result. Killswitch ARTA_R297_DTO_EXTRACT_DISABLE=1.
    """
    import os as _os
    if _os.environ.get("ARTA_R297_DTO_EXTRACT_DISABLE") == "1":
        return {}
    pid = str((project or {}).get("id") or "")
    if not pid:
        return {}
    try:
        import json as _json
        from .api_discovery import _load_captured_endpoints
        from .test_data import build_request_bodies

        caps = _load_captured_endpoints(pid) or []
        # only endpoints that ALREADY lack a non-empty body — DTO extraction is
        # the fallback source, never overrides real observed/contract bodies.
        _spec = {}
        _ope = _Path(".arta/openapi") / f"{pid}.json"
        if _ope.is_file():
            try:
                _spec = _json.loads(_ope.read_text())
            except Exception:
                _spec = {}
        _existing = build_request_bodies(openapi_spec=_spec, captured_endpoints=caps,
                                         project_id=pid) or {}

        def _nonempty(b):
            f = b[0] if isinstance(b, list) and b else b
            return isinstance(f, dict) and bool(f)

        _bodyless = [e for e in caps if isinstance(e, dict)
                     and str(e.get("method") or "").upper() in ("POST", "PUT", "PATCH")
                     and not _nonempty(_existing.get(
                         f"{str(e.get('method')).upper()} {e.get('path')}") or {})]
        if not _bodyless:
            return {}

        # endpoint key -> declaring-requirement text (R277 stamps declared_by).
        req_by_id = {}
        try:
            reqs = _json.loads((_Path(".arta/requirements.json")).read_text()).get(pid) or []
            for r in reqs:
                rid = r.get("req_id") or r.get("id")
                if rid:
                    req_by_id[str(rid)] = "%s %s" % (
                        r.get("description") or "", _json.dumps(r.get("acceptance_criteria") or []))
        except Exception:
            req_by_id = {}
        _text_by_key = {}
        for e in _bodyless:
            _key = f"{str(e.get('method')).upper()} {e.get('path')}"
            _text_by_key[_key] = req_by_id.get(str(e.get("declared_by") or ""), "")

        extracted = await build_dto_request_bodies(project, _bodyless, _text_by_key)
        if extracted:
            persist_dto_request_bodies(pid, {**load_dto_request_bodies(pid), **extracted})
        return extracted
    except Exception as exc:
        log.debug("R297: refresh_dto_request_bodies failed for %s: %s", pid, exc)
        return {}
