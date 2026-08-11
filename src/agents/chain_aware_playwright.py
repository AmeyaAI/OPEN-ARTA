"""R211 Phase D — chain-aware Playwright generation (the PW mirror of
`chain_aware_newman`).

Generates a deterministic `.spec.ts` from an ORDERED chain (R200 cm hierarchy /
R195 FK pipeline / a captured CallChain). Playwright previously had ONLY
prompt-hint workflow blocks (R193/R195/R200) the LLM had to hand-code and
didn't reliably — so cm reads needing a real `{collection_id}` from a list 500'd.

READ-MODE (default, R154-safe — GET only): each step GETs a parent list →
captures a REAL id from the response (defensive multi-shape) → threads it into
the next step's path → GETs the target. This solves the
`{collection_id}`-from-a-list problem deterministically so reads reach 2xx.

Composes with R207/R210: paths route via `apiUrlFor(...)` (multi-host) and auth
via `authHeaderFor(...)` (per-family) — no new auth logic here.

ACTION-MODE (`read_only=False`, Phase E — R154-gated): steps may POST/PUT with
synthesized bodies; only emitted behind the `@intentional-destructive` marker +
sandbox env (handled by the caller).

Node shape (mirrors chain_aware_newman / CallNode):
    {method, path_template, sequence_index, provides:{var: "$.json.path"},
     consumes:{var: provider_idx}}
"""
from __future__ import annotations

import json
import logging
import re

from .chain_aware_newman import _r138_a_should_skip_chain_node

log = logging.getLogger("arta.chain_aware_playwright")

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _jsonpath_to_ts(path: str) -> str:
    """Convert a simple `$.items[0].id` JSONPath to a defensive TS accessor
    chain on `_b`, e.g. `_b?.items?.[0]?.id`. Falls back across common list
    shapes so a slightly-different SUT response still yields the id."""
    parts = [p for p in re.sub(r"^\$\.?", "", path or "").split(".") if p]
    if not parts:
        return "_b"
    acc = "_b"
    for seg in parts:
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]$", seg)
        if m:
            acc += f"?.{m.group(1)}?.[{m.group(2)}]"
        elif seg.endswith("[0]"):
            acc += f"?.{seg[:-3]}?.[0]"
        else:
            acc += f"?.{seg}"
    return acc


def _render_path_ts(path_template: str, known_ids: dict, consumed: set) -> str:
    """Render a path template into a TS template-literal BODY (no backticks).
    `{param}` → the concrete session id from `known_ids`, else `${_vars['param']}`
    (a chain-captured/consumed id threaded at runtime)."""
    def _sub(m: re.Match) -> str:
        p = m.group(1)
        if p in (known_ids or {}):
            return str(known_ids[p])
        return "${_vars['" + p + "']}"
    body = _PARAM_RE.sub(_sub, path_template or "/")
    if not body.startswith("/"):
        body = "/" + body
    return body


_R217_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_R217_CM_QP = "filters=null&last_evaluated_key=null&page_size=1000&include_detail=false"


def _r217_cm_fixup(path: str, known_ids: dict) -> tuple[str, bool]:
    """R217 — port the newman cm fixes to the PW chain path:
      (1) rewrite the account-position segments (leading + the slot right after
          `collection`, when it equals the leading account) to the LIVE
          account_id. The chain baked a STALE account (`0aee6bd7…`) but the
          SUT's cm reads key on the current root account (`955934e1…`) → 500.
      (2) flag cm-LIST reads (`…/cm/v<N>/<namespace>/<type>` ending in a
          non-UUID type) so the caller appends the required LIST query params
          (filters/last_evaluated_key/page_size/include_detail) — without them
          the SUT 500s. Mirrors newman's account-override + cm-position
          templating + R160.B query-param injection. Killswitch
          ARTA_R217_PW_CM_FIXUP_DISABLE=1.
    """
    import os as _os
    if _os.environ.get("ARTA_R217_PW_CM_FIXUP_DISABLE") == "1":
        return path, False
    if "/api/collection/" not in path or "/cm/" not in path:
        return path, False
    # account_id, falling back to root_account_id (== account for the root
    # values / the PW emit's known_ids may lack a plain account_id).
    acct = (known_ids or {}).get("account_id") or (known_ids or {}).get("root_account_id")
    segs = path.split("/")
    nonempty = [i for i, s in enumerate(segs) if s]
    if acct and nonempty:
        lead_i = nonempty[0]
        orig_lead = segs[lead_i]
        if _R217_UUID_RE.match(segs[lead_i] or ""):
            segs[lead_i] = acct
        for i, s in enumerate(segs):
            if s == "collection" and i + 1 < len(segs) and segs[i + 1] == orig_lead:
                segs[i + 1] = acct
        path = "/".join(segs)
    _last = next((s for s in reversed(path.split("/")) if s), "")
    is_cm_list = ("composite_default" in path and bool(_last)
                  and not _R217_UUID_RE.match(_last))
    return path, is_cm_list


def _capture_block(provides: dict, *, indent: str, step_label: str) -> list[str]:
    """Emit the provides-capture lines: walk each JSONPath into `_vars`, with a
    multi-shape fallback, and cascade-skip when a required id is empty."""
    lines: list[str] = []
    if not provides:
        return lines
    lines.append(f"{indent}const _b = await _r.json().catch(() => ({{}}));")
    for var, jpath in provides.items():
        acc = _jsonpath_to_ts(jpath)
        # multi-shape fallback: explicit path → items[0].id → data[0].id → [0].id
        # (dedupe so an explicit `$.items[0].id` doesn't repeat the first fallback).
        _chain = [acc] + [f for f in ("_b?.items?.[0]?.id", "_b?.data?.[0]?.id",
                                      "_b?.[0]?.id") if f != acc]
        fallback = " ?? ".join(_chain) + " ?? ''"
        lines.append(f"{indent}_vars['{var}'] = String({fallback});")
        lines.append(
            f"{indent}if (!_vars['{var}']) {{ test.skip(true, "
            f"'cascade_skip: {step_label} provided no {var}'); return; }}")
    return lines


_NAME_FIELDS = ("name", "title", "label", "slug", "display_name")


def _body_for_node(node: dict, bodies: dict, namespace_env: str) -> str:
    """Render the synthesized request body (Phase C) for an action node as a TS
    object literal, namespace-prefixing name/title fields (template literals) so
    created resources are scoped to SUT_TEST_DATA_NAMESPACE (R154 sandbox)."""
    method = (node.get("method") or "GET").upper()
    path = node.get("path_template") or ""
    body = (bodies or {}).get(f"{method} {path}") or (bodies or {}).get(path) or {}
    if not isinstance(body, dict) or not body:
        return "{}"
    parts: list[str] = []
    for k, v in body.items():
        if str(k).lower() in _NAME_FIELDS and isinstance(v, str):
            val = ("`${process.env." + namespace_env
                   + " || 'arta-test'}-" + v + "`")
        else:
            val = json.dumps(v)
        parts.append(f"{json.dumps(str(k))}: {val}")
    return "{ " + ", ".join(parts) + " }"


def build_spec_from_chain(
    chain: dict,
    *,
    spec_name: str,
    req_id: str,
    known_ids: dict | None = None,
    read_only: bool = True,
    bodies: dict | None = None,
    namespace_env: str = "SUT_TEST_DATA_NAMESPACE",
) -> str:
    """R211 Phase D/E — emit a deterministic chained `.spec.ts` string.

    READ-MODE (default, R154-safe): GET-only — each step GETs a parent list,
    captures a REAL id, threads it forward, GETs the target.

    ACTION-MODE (`read_only=False`, Phase E — caller MUST gate on the AC
    `mutation.destructive` flag + R154 env `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1`
    + `SUT_TEST_DATA_NAMESPACE`): POST/PUT steps carry Phase-C synthesized bodies
    (namespace-prefixed names), capture created ids, thread them forward. The
    emitted spec carries the `@intentional-destructive` marker on line 1 so the
    R154.B validator exempts it and R154.C re-checks the env at dispatch.

    Returns "" when the chain has no testable node (caller falls back to the LLM
    spec).
    """
    known_ids = known_ids or {}
    nodes = [n for n in (chain.get("nodes") or []) if isinstance(n, dict)]
    nodes.sort(key=lambda n: int(n.get("sequence_index") or 0))

    steps: list[dict] = []
    skipped: dict[str, int] = {}
    for n in nodes:
        skip, reason = _r138_a_should_skip_chain_node(n)
        if skip:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        steps.append(n)
    if not steps:
        return ""

    _WRITE = {"POST", "PUT", "PATCH"}

    def _emit_step(i: int, n: dict, ind: str) -> list[str]:
        """Emit ONE step's body lines (GET/WRITE + assert + provides-capture)."""
        method = (n.get("method") or "GET").upper()
        path_tmpl = n.get("path_template") or "/"
        consumed = set((n.get("consumes") or {}).keys())
        path_ts = _render_path_ts(path_tmpl, known_ids, consumed)
        # R217 — cm account rewrite (stale→live) + cm-LIST query-param append.
        path_ts, _is_cm_list = _r217_cm_fixup(path_ts, known_ids)
        if _is_cm_list and "?" not in path_ts:
            path_ts = f"{path_ts}?{_R217_CM_QP}"
        label = f"step_{i:02d}_{method}"
        out = [f"{ind}// {label}: {method} {path_tmpl}",
               f"{ind}{{",
               f"{ind}  const _u = `{path_ts}`;"]
        if method in _WRITE and not read_only:
            _data = _body_for_node(n, bodies or {}, namespace_env)
            out.append(f"{ind}  const _r = await request.{method.lower()}(apiUrlFor(_u), "
                       f"{{ headers: authHeaderFor(_u), data: {_data}, timeout: 30000 }});")
            out.append(f"{ind}  expect([200, 201, 204]).toContain(_r.status());")
        else:
            out.append(f"{ind}  const _r = await request.get(apiUrlFor(_u), "
                       f"{{ headers: authHeaderFor(_u), timeout: 30000 }});")
            out.append(f"{ind}  expect(_r.status()).toBe(200);")
        out.extend(_capture_block(n.get("provides") or {}, indent=ind + "  ", step_label=label))
        out.append(f"{ind}}}")
        return out

    # dispatchable steps (read-mode drops non-GET)
    dispatch_steps = []
    for i, n in enumerate(steps):
        if read_only and (n.get("method") or "GET").upper() != "GET":
            skipped["non_get_read_mode"] = skipped.get("non_get_read_mode", 0) + 1
            continue
        dispatch_steps.append((i, n))
    if not dispatch_steps:
        return ""

    # R217 — chain-brittleness fix: in read-mode, ISOLATE independent steps into
    # their own test() so one SUT-500 read (e.g. a genuinely-broken cm collection)
    # doesn't fail its PASSING siblings. A step is isolatable iff it neither
    # consumes a threaded var NOR provides one a sibling consumes (a standalone
    # read, e.g. a cm LIST). Genuinely-threaded steps stay chained (need _vars).
    # Killswitch ARTA_R217_PW_SPLIT_CHAIN_DISABLE=1 → single combined test (pre-R217).
    import os as _os_split
    _split = read_only and _os_split.environ.get("ARTA_R217_PW_SPLIT_CHAIN_DISABLE") != "1"
    _consumed_all: set = set()
    for _i, _n in dispatch_steps:
        _consumed_all |= set((_n.get("consumes") or {}).keys())

    def _isolatable(n: dict) -> bool:
        if (n.get("consumes") or {}):
            return False
        return not (set((n.get("provides") or {}).keys()) & _consumed_all)

    mode = "read-mode" if read_only else "action-mode"
    test_blocks: list[str] = []
    chained: list[str] = []
    for i, n in dispatch_steps:
        if _split and _isolatable(n):
            _ttl = f"step {i:02d}: {(n.get('method') or 'GET').upper()} {(n.get('path_template') or '/')[:80]}"
            blk = [f"  test('{_ttl}', async ({{ request }}) => {{",
                   "    const _vars: Record<string, string> = {};"]
            blk += _emit_step(i, n, "    ")
            blk.append("  });")
            test_blocks.append("\n".join(blk))
        else:
            chained.extend(_emit_step(i, n, "    "))
    if chained:
        blk = [f"  test('workflow chain: {req_id}', async ({{ request }}) => {{",
               "    const _vars: Record<string, string> = {};"]
        blk += chained
        blk.append("  });")
        test_blocks.append("\n".join(blk))
    if not test_blocks:
        return ""

    header: list[str] = []
    if not read_only:
        # R154 opt-in marker MUST be in the first 5 lines (_r154_b_has_opt_in_marker).
        header.append(f"// @intentional-destructive: {req_id} action workflow "
                      f"(namespace-scoped via {namespace_env}; R154.C re-checks env)")
    header += [
        f"// R211 Phase D/E — DETERMINISTIC chain-aware Playwright spec ({mode}).",
        f"// requirement: {req_id}; generated from the SUT-grounded workflow chain.",
        ("// GET-only, no mutation (R154-safe). R217: independent reads isolated."
         if read_only else
         "// create→capture→create-on-it; namespace-scoped; R154-gated."),
        "import { test, expect } from '@playwright/test';",
        "import { apiUrlFor, authHeaderFor } from '../common/arta_auth';",
        "",
        f"test.describe('{spec_name} — chained API contract ({mode})', () => {{",
    ]
    footer = ["});", ""]
    if skipped:
        header.insert(0, f"// R138.A/{mode} skipped: {json.dumps(skipped)}")
        log.info("chain_aware_playwright: %s skipped %s", req_id, skipped)
    return "\n".join(header + test_blocks + footer)


__all__ = ["build_spec_from_chain"]
