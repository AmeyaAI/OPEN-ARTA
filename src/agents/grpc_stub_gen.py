"""gRPC stub generation — compile discovered `.proto` files into importable
Python stubs so a generated gRPC test's `from .stubs import <n>_pb2, <n>_pb2_grpc`
resolves at runtime.

This is the BUILD step that pairs with:
  - `github_context.fetch_files_by_extension(project, ".proto")` (fetch, step 3),
  - `protocol_discovery.parse_proto` / `build_protocol_nodes` (understand),
  - the R156.E gRPC gen constraint + `arta_runtime.grpc_helpers.GrpcClient` (run).

Deterministic, no network. Requires `grpcio-tools` (protoc). Fail-loud: returns
the protoc errors rather than raising, so a partial compile is visible.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("arta.grpc_stub_gen")

_GRPC_SURFACE_DIR = Path(".arta/grpc")

# READ-side gRPC method prefixes (R154 non-mutation). MIRRORS the canonical
# `_R156_E_READ_METHOD_PREFIXES` in arta_runtime/grpc_helpers.py (kept in sync;
# duplicated only to avoid importing a runtime module into the agents layer).
_READ_METHOD_PREFIXES = (
    "get", "list", "read", "inspect", "describe", "verify", "validate", "check",
    "query", "search", "find", "lookup", "fetch", "show", "view", "stream",
)


def _is_read_method(name: str) -> bool:
    n = (name or "").lower()
    return any(n.startswith(p) for p in _READ_METHOD_PREFIXES)

# protoc's grpc_python_out emits an ABSOLUTE sibling import (`import foo_pb2 as
# foo__pb2`) that only resolves when the output dir is on sys.path. Rewriting it
# to a PACKAGE-relative import makes the stubs dir a self-contained package, so
# `from .stubs import foo_pb2, foo_pb2_grpc` works from the generated test.
_ABS_PB2_IMPORT = re.compile(r"^import (\w+_pb2) as (\w+)$", re.M)


def compile_protos(proto_files: list[dict], out_dir: str | Path) -> dict:
    """Compile `.proto` files into `<stem>_pb2.py` + `<stem>_pb2_grpc.py` in
    `out_dir` (made an importable package via `__init__.py`).

    `proto_files` = [{path, text}] (as returned by fetch_files_by_extension).
    Protos are staged flat by basename (so cross-file `import "x.proto"` by
    basename resolves) and compiled with a single include root. The grpc output's
    absolute `import x_pb2` is rewritten to `from . import x_pb2` so the package
    is self-contained.

    Returns {modules: [<stem>...], services: bool-per-stem, out_dir, errors}.
    Killswitch ARTA_GRPC_STUB_GEN_DISABLE → {disabled: True}. Deterministic."""
    if os.environ.get("ARTA_GRPC_STUB_GEN_DISABLE") == "1":
        return {"disabled": True, "modules": [], "out_dir": str(out_dir), "errors": []}
    out = Path(out_dir)
    errors: list[str] = []
    if not proto_files:
        return {"modules": [], "out_dir": str(out), "errors": ["no_proto_files"]}
    try:
        from grpc_tools import protoc as _protoc
    except Exception as exc:  # grpcio-tools not installed
        return {"modules": [], "out_dir": str(out),
                "errors": [f"grpcio-tools_unavailable: {exc}"]}

    import tempfile
    out.mkdir(parents=True, exist_ok=True)
    (out / "__init__.py").write_text("")
    modules: list[str] = []

    with tempfile.TemporaryDirectory() as _root:
        root = Path(_root)
        staged: list[tuple[str, str]] = []  # (basename, stem)
        _seen_names: set[str] = set()
        for pf in proto_files:
            name = Path(pf.get("path") or "").name
            if not name.endswith(".proto") or not pf.get("text"):
                continue
            # Protos are staged flat by basename (so cross-file `import "x.proto"`
            # resolves). Two protos sharing a basename would silently overwrite —
            # keep the first, skip + record the collision (fail-loud, not silent).
            if name in _seen_names:
                errors.append(f"basename_collision_skipped:{pf.get('path') or name}")
                log.warning("compile_protos: duplicate basename %r (%s) — kept the "
                            "first, skipped this one", name, pf.get("path"))
                continue
            _seen_names.add(name)
            (root / name).write_text(pf["text"])
            staged.append((name, name[:-len(".proto")]))
        if not staged:
            return {"modules": [], "out_dir": str(out), "errors": ["no_valid_proto"]}
        for name, stem in staged:
            rc = _protoc.main([
                "protoc", f"-I{root}",
                f"--python_out={out}", f"--grpc_python_out={out}",
                str(root / name),
            ])
            if rc != 0:
                errors.append(f"protoc_rc={rc}:{name}")
                continue
            modules.append(stem)

    # Rewrite absolute pb2 imports in the *_pb2_grpc.py files → package-relative.
    services: dict[str, bool] = {}
    for stem in modules:
        grpc_py = out / f"{stem}_pb2_grpc.py"
        if grpc_py.is_file():
            txt = grpc_py.read_text()
            txt = _ABS_PB2_IMPORT.sub(r"from . import \1 as \2", txt)
            grpc_py.write_text(txt)
            services[stem] = "Stub(" in txt or "add_" in txt  # a servicer/stub exists
        else:
            services[stem] = False
    return {"modules": sorted(modules), "services": services,
            "out_dir": str(out), "errors": errors}


# Default landing dir: a `stubs` package next to where the pytest runner globs
# gRPC/analytics specs, so a generated `from .stubs import <n>_pb2, <n>_pb2_grpc`
# resolves. Runtime-generated (gitignored); NOT a tracked artifact.
DEFAULT_STUB_DIR = "src/automation/python_tests/analytics/stubs"


async def generate_project_grpc_stubs(project: dict, *, out_dir: str | None = None) -> dict:
    """Fetch a project's `.proto` from its configured repos (git-tree walk) and
    compile them into importable stubs. Composes the two proven halves:
    `github_context.fetch_files_by_extension` + `compile_protos`. Returns the
    compile result augmented with `proto_count`. `{proto_count: 0}` when no
    `.proto` is reachable (no repos / token) — fail-open, caller decides."""
    from .github_context import fetch_files_by_extension
    proto_files = await fetch_files_by_extension(project, ".proto", cap=20)
    if not proto_files:
        return {"proto_count": 0, "modules": [], "out_dir": out_dir or DEFAULT_STUB_DIR,
                "errors": ["no_proto_reachable"]}
    res = compile_protos(proto_files, out_dir or DEFAULT_STUB_DIR)
    res["proto_count"] = len(proto_files)
    return res


# ── Durable gRPC SURFACE (understanding → feeds gen) ──────────────────────────

def build_grpc_surface(proto_files: list[dict]) -> dict:
    """A durable, gen-ready description of the SUT's gRPC surface from its
    `.proto` files: per service the exact stub imports + methods with request/
    response message names + a READ-side flag (R154). `proto_files` = [{path,
    text}] (from fetch_files_by_extension). Deterministic; reuses parse_proto.

    Returns {services: [{name, stub_pb2, stub_grpc, stub_class, methods:[{name,
    request, response, streaming, read_side}]}], stub_modules: [...]}."""
    from .protocol_discovery import parse_proto
    services: list[dict] = []
    stub_modules: list[str] = []
    for pf in (proto_files or []):
        stem = Path(pf.get("path") or "").stem
        if not stem or not pf.get("text"):
            continue
        parsed = parse_proto(pf["text"])
        if not parsed.get("services"):
            continue
        pb2, pb2_grpc = f"{stem}_pb2", f"{stem}_pb2_grpc"
        for m in (pb2, pb2_grpc):
            if m not in stub_modules:
                stub_modules.append(m)
        for svc in parsed["services"]:
            services.append({
                "name": svc["name"], "stub_pb2": pb2, "stub_grpc": pb2_grpc,
                "stub_class": f"{svc['name']}Stub",
                "methods": [{"name": mth["name"], "request": mth.get("request"),
                             "response": mth.get("response"),
                             "streaming": bool(mth.get("streaming")),
                             "read_side": _is_read_method(mth["name"])}
                            for mth in (svc.get("methods") or [])],
            })
    return {"services": services, "stub_modules": sorted(set(stub_modules))}


def persist_grpc_surface(project_id: str, surface: dict) -> None:
    """Write the gRPC surface to `.arta/grpc/<pid>.json` (only when non-empty)."""
    if not project_id or not (surface or {}).get("services"):
        return
    _GRPC_SURFACE_DIR.mkdir(parents=True, exist_ok=True)
    (_GRPC_SURFACE_DIR / f"{project_id}.json").write_text(json.dumps(surface, indent=2))


def load_grpc_surface(project_id: str) -> dict:
    """Read the persisted gRPC surface; {} when absent/unreadable."""
    p = _GRPC_SURFACE_DIR / f"{project_id}.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def grpc_surface_prompt_block(project_id: str, *, max_services: int = 8) -> str:
    """Gen-grounding block naming the SUT's REAL gRPC services/methods/messages +
    the exact stub imports, so gen emits a correct test instead of the R156.E
    placeholder example. READ-side methods flagged (R154). "" when no surface.
    Killswitch ARTA_GRPC_GROUNDING_DISABLE."""
    if os.environ.get("ARTA_GRPC_GROUNDING_DISABLE") == "1":
        return ""
    services = (load_grpc_surface(project_id) or {}).get("services") or []
    if not services:
        return ""
    lines = [
        "[gRPC SURFACE (discovered from the SUT's .proto — use these EXACT names)]",
        "Use the canonical client `from src.automation.python_tests.arta_runtime."
        "grpc_helpers import GrpcClient` + the compiled stubs. Call READ-side (read)",
        "methods by default; write methods need the R154 destructive opt-in.",
    ]
    for svc in services[:max_services]:
        lines.append(
            f"  service {svc['name']}  "
            f"(from .stubs import {svc['stub_pb2']}, {svc['stub_grpc']}; "
            f"stub = {svc['stub_grpc']}.{svc['stub_class']})")
        for m in (svc.get("methods") or [])[:12]:
            flag = "read" if m.get("read_side") else "write(opt-in)"
            lines.append(f"      rpc {m['name']}({m.get('request')}) "
                         f"-> {m.get('response')}  [{flag}]")
    lines.append("Build the request via <stub_pb2>.<RequestMessage>(...) and call "
                 "client.call(<stub_grpc>.<Service>Stub, '<Method>', req).")
    return "\n".join(lines) + "\n"


# ── Deterministic gRPC read-side test emitter (no LLM) ────────────────────────

def build_grpc_read_test(surface: dict, *, target_env: str = "GRPC_TARGET",
                         stub_import: str = "stubs") -> str:
    """A runnable pytest that read-smoke-tests every discovered READ-side rpc
    (R154: reads only; write methods are omitted — no `allow_destructive`). Each
    test builds a default request, calls via GrpcClient, and treats the endpoint
    as reachable unless it returns UNIMPLEMENTED/UNAVAILABLE (transport/missing) —
    application errors on the empty request still prove the endpoint EXISTS
    (mirrors the Newman "not 5xx" / GraphQL "no errors" grounded-smoke pattern).
    Deterministic; imports resolve against `compile_protos` output. "" when the
    surface has no read-side rpc."""
    services = (surface or {}).get("services") or []
    read = [(svc, m) for svc in services for m in (svc.get("methods") or [])
            if m.get("read_side")]
    if not read:
        return ""
    pb2_mods = sorted({s["stub_pb2"] for s, _ in read} | {s["stub_grpc"] for s, _ in read})
    out = [
        '"""Auto-generated gRPC READ-side smoke tests (deterministic, R154 read-only).',
        "Each verifies a discovered read rpc is REACHABLE; application-level errors on",
        'the empty request are tolerated, UNIMPLEMENTED/UNAVAILABLE are failures."""',
        "import os",
        "import grpc",
        "from src.automation.python_tests.arta_runtime.grpc_helpers import GrpcClient",
        f"from .{stub_import} import " + ", ".join(pb2_mods),
        "",
        f'_TARGET = os.environ.get({target_env!r}, "localhost:50051")',
        "_UNREACHABLE = (grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.UNAVAILABLE)",
        "",
    ]
    seen: set[str] = set()
    for svc, m in read:
        fn = f"test_{svc['name']}_{m['name']}".lower()
        if fn in seen:
            fn = f"{fn}_{len(seen)}"
        seen.add(fn)
        label = f"{svc['name']}.{m['name']}"
        out += [
            f"def {fn}():",
            f"    with GrpcClient(_TARGET) as client:",
            f"        req = {svc['stub_pb2']}.{m['request']}()",
            "        try:",
            f"            resp = client.call({svc['stub_grpc']}.{svc['stub_class']}, "
            f"{m['name']!r}, req)",
            "            assert resp is not None",
            "        except grpc.RpcError as e:",
            f"            assert e.code() not in _UNREACHABLE, "
            f"{label!r} + ' unreachable: ' + str(e.code())",
            "",
        ]
    return "\n".join(out)


async def generate_project_grpc_tests(project: dict, *, tests_dir: str | None = None,
                                      stubs_subdir: str = "stubs") -> dict:
    """The full DETERMINISTIC gRPC gen chain for a project: fetch `.proto`
    (tree-walk) → compile stubs into `<tests_dir>/<stubs_subdir>` → build the
    surface → emit a read-side smoke test to `<tests_dir>/test_grpc_smoke.py`.
    Returns {proto_count, stub_modules, read_tests, test_path, errors}. No LLM.
    `{proto_count: 0}` when no `.proto` is reachable (fail-open)."""
    from .github_context import fetch_files_by_extension
    base = Path(tests_dir or str(Path(DEFAULT_STUB_DIR).parent))
    proto_files = await fetch_files_by_extension(project, ".proto", cap=20)
    if not proto_files:
        return {"proto_count": 0, "read_tests": 0, "errors": ["no_proto_reachable"]}
    stub_res = compile_protos(proto_files, base / stubs_subdir)
    surface = build_grpc_surface(proto_files)
    test_src = build_grpc_read_test(surface, stub_import=stubs_subdir)
    result = {"proto_count": len(proto_files), "stub_modules": stub_res.get("modules", []),
              "read_tests": test_src.count("\ndef test_"), "errors": stub_res.get("errors", [])}
    if test_src:
        base.mkdir(parents=True, exist_ok=True)
        test_path = base / "test_grpc_smoke.py"
        test_path.write_text(test_src)
        result["test_path"] = str(test_path)
    return result
