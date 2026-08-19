"""gRPC stub generation — compile discovered .proto into importable stubs so a
generated `from .stubs import <n>_pb2, <n>_pb2_grpc` resolves. Uses an inline
proto (no network); requires grpcio-tools (a declared dependency)."""
import importlib
import sys

import pytest

from src.agents.grpc_stub_gen import compile_protos

pytest.importorskip("grpc_tools", reason="grpcio-tools (protoc) required")

_PROTO = {
    "path": "src/app/authorization.proto",
    "text": (
        'syntax = "proto3";\n'
        "package auth;\n"
        "service AuthorizationService {\n"
        "  rpc Authenticate (AuthenticationRequest) returns (AuthenticationResponse);\n"
        "}\n"
        "message AuthenticationRequest { string token = 1; }\n"
        "message AuthenticationResponse { bool is_valid = 1; }\n"
    ),
}


def test_compile_produces_importable_stubs(tmp_path):
    out = tmp_path / "stubs"
    res = compile_protos([_PROTO], out)
    assert res["errors"] == []
    assert res["modules"] == ["authorization"]
    assert res["services"]["authorization"] is True
    # the package is importable and carries the REAL service stub + message
    assert (out / "authorization_pb2.py").is_file()
    assert (out / "authorization_pb2_grpc.py").is_file()
    assert (out / "__init__.py").is_file()
    # the grpc file's cross-module import was rewritten package-relative
    grpc_src = (out / "authorization_pb2_grpc.py").read_text()
    assert "from . import authorization_pb2" in grpc_src
    assert "\nimport authorization_pb2" not in grpc_src

    sys.path.insert(0, str(tmp_path))
    try:
        pb2 = importlib.import_module("stubs.authorization_pb2")
        pb2_grpc = importlib.import_module("stubs.authorization_pb2_grpc")
        assert hasattr(pb2, "AuthenticationRequest")
        assert hasattr(pb2_grpc, "AuthorizationServiceStub")
    finally:
        sys.path.remove(str(tmp_path))


def test_no_proto_files(tmp_path):
    res = compile_protos([], tmp_path / "stubs")
    assert res["modules"] == [] and res["errors"] == ["no_proto_files"]


def test_killswitch(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_GRPC_STUB_GEN_DISABLE", "1")
    assert compile_protos([_PROTO], tmp_path / "stubs")["disabled"] is True


# ── gRPC SURFACE (understanding → feeds gen) ──────────────────────────────────

from src.agents import grpc_stub_gen as gsg  # noqa: E402

_SURFACE_PROTO = {
    "path": "src/app/authorization.proto",
    "text": (
        'syntax = "proto3";\n'
        "service AuthorizationService {\n"
        "  rpc Authenticate (AuthenticationRequest) returns (AuthenticationResponse);\n"
        "  rpc GetPolicy (GetPolicyRequest) returns (Policy);\n"
        "  rpc CreatePrimaryPolicy (CreatePrimaryPolicyRequest) returns (Policy);\n"
        "}\n"
    ),
}


def test_build_surface_classifies_read_side():
    surface = gsg.build_grpc_surface([_SURFACE_PROTO])
    svc = surface["services"][0]
    assert svc["name"] == "AuthorizationService"
    assert svc["stub_pb2"] == "authorization_pb2"
    assert svc["stub_grpc"] == "authorization_pb2_grpc"
    assert svc["stub_class"] == "AuthorizationServiceStub"
    read = {m["name"]: m["read_side"] for m in svc["methods"]}
    assert read["GetPolicy"] is True                    # get* → read
    assert read["Authenticate"] is False                # not a read prefix
    assert read["CreatePrimaryPolicy"] is False         # create* → write
    assert "authorization_pb2_grpc" in surface["stub_modules"]


def test_persist_load_and_prompt_block(tmp_path, monkeypatch):
    monkeypatch.setattr(gsg, "_GRPC_SURFACE_DIR", tmp_path)
    gsg.persist_grpc_surface("pid", gsg.build_grpc_surface([_SURFACE_PROTO]))
    assert gsg.load_grpc_surface("pid")["services"][0]["name"] == "AuthorizationService"
    blk = gsg.grpc_surface_prompt_block("pid")
    # names the real service + exact stub imports + the read/write flags
    assert "AuthorizationService" in blk
    assert "from .stubs import authorization_pb2, authorization_pb2_grpc" in blk
    assert "GetPolicy" in blk and "[read]" in blk
    assert "CreatePrimaryPolicy" in blk and "write(opt-in)" in blk


def test_prompt_block_empty_and_killswitch(tmp_path, monkeypatch):
    monkeypatch.setattr(gsg, "_GRPC_SURFACE_DIR", tmp_path)
    assert gsg.grpc_surface_prompt_block("absent") == ""          # no surface
    gsg.persist_grpc_surface("pid", gsg.build_grpc_surface([_SURFACE_PROTO]))
    monkeypatch.setenv("ARTA_GRPC_GROUNDING_DISABLE", "1")
    assert gsg.grpc_surface_prompt_block("pid") == ""             # killswitch


# ── Deterministic gRPC read-test emitter ──────────────────────────────────────

def test_emitter_reads_only_and_valid_python():
    import ast
    surface = gsg.build_grpc_surface([_SURFACE_PROTO])
    src = gsg.build_grpc_read_test(surface)
    ast.parse(src)                                               # syntactically valid
    # only the read-side method emits a test; write/non-read methods omitted (R154)
    assert "def test_authorizationservice_getpolicy" in src
    assert "def test_authorizationservice_authenticate" not in src   # not a read prefix
    assert "createprimarypolicy" not in src.lower()                  # write, omitted
    # references the REAL stub + method + package-relative stub import
    assert "from .stubs import authorization_pb2, authorization_pb2_grpc" in src
    assert "authorization_pb2_grpc.AuthorizationServiceStub" in src
    assert "'GetPolicy'" in src
    assert src.count("\ndef test_") == 1                         # exactly the 1 read method


def test_emitter_empty_when_no_read_methods():
    # a proto whose only method is a write → no read-side test emitted
    write_only = {"path": "x/w.proto", "text":
                  'service S { rpc CreateThing (Req) returns (Resp); }\n'}
    surface = gsg.build_grpc_surface([write_only])
    assert gsg.build_grpc_read_test(surface) == ""
