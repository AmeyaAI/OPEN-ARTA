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

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("arta.grpc_stub_gen")

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
        for pf in proto_files:
            name = Path(pf.get("path") or "").name
            if not name.endswith(".proto") or not pf.get("text"):
                continue
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
