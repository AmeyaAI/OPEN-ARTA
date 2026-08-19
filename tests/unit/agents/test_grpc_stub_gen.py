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
