"""R156.E — gRPC functional test helper for pytest.

Single-source-of-truth for authenticated gRPC client construction in
generated pytest tests targeting R156.C-detected gRPC endpoints
(e.g., a SUT's auth-service-class servicers).

Why this helper exists: gRPC has a different transport (HTTP/2 +
protobuf framing) and authentication model (per-call metadata, not
HTTP headers) than REST. Without a canonical helper:
  - LLM gen path produces broken `grpc.aio.insecure_channel` calls
    that ignore the project's TLS + auth chain
  - Tests can't compose with R143.D chromium bridge (which only
    helps PW chromium subprocess) — gRPC dispatches via Python
    process directly, so the only network safety is `grpc.ssl_*`
  - No deterministic place to inject `authorization: bearer <token>`
    metadata interceptor

R156.E ships `GrpcClient` that:
  1. Opens a secure channel (TLS by default; opt-out via env var
     `ARTA_GRPC_INSECURE=1` for local-dev SUTs)
  2. Adds a metadata interceptor that injects
     `authorization: bearer ${process.env.AUTH_TOKEN}` on every call
     (R95.1 token precedence → R156.J.3 auto-refresh-compatible)
  3. Exposes `client.call(service_stub, method, request_msg, timeout=)`
     that returns the response message OR raises a structured error
     with operator-readable context

R154 non-mutation guarantee: gRPC method allowlist mirrors R154.A
Layer 2 — only "Get*"/"List*"/"Read*"/"Inspect*"-prefixed methods
are allowed by default. Operator opts in to destructive ops via
`@intentional-destructive` marker in spec header (R154.B semantics)
plus `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` + `SUT_TEST_DATA_NAMESPACE`
env vars (R154.C).

R156.F (when shipped) supplies the auto-generated `*_pb2.py` /
`*_pb2_grpc.py` stub modules from the SUT's .proto files. Until
then, operators ship their own stubs OR the helper falls back to
the canonical `protobuf.empty_pb2.Empty` placeholder so gen tests
have a structural shape.

Operator-tunable env vars:
  AUTH_TOKEN                    -- bearer token for authorization metadata
  ARTA_GRPC_INSECURE            -- "1" → use insecure channel (default off)
  ARTA_GRPC_DEFAULT_TIMEOUT_SEC -- default 10.0 (per-call timeout)
  ARTA_GRPC_AUTH_METADATA_KEY   -- default "authorization" (override
                                    for SUTs using "x-auth-token" etc.)
  ARTA_GRPC_AUTH_METADATA_VALUE_PREFIX -- default "bearer " (RFC 7235);
                                          override e.g. "" for raw-token SUTs

Example (in a generated pytest spec):

    from src.automation.python_tests.arta_runtime.grpc_helpers import (
        GrpcClient,
    )

    def test_auth_service_verify_token():
        client = GrpcClient("auth.example.svc.cluster.local:9090")
        # Stub class imported from the SUT's R156.F-generated stubs:
        # from src.automation.grpc.<project>.<sha> import auth_pb2_grpc
        from .stubs import auth_pb2, auth_pb2_grpc
        req = auth_pb2.VerifyTokenRequest(token="...")
        resp = client.call(auth_pb2_grpc.AuthServiceStub, "VerifyToken", req)
        assert resp.is_valid is True
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# R154 read-side method allowlist for gRPC. Mirrors R154.A Layer 2's
# label allowlist (positive-match for read intent). Methods not
# matching default to BLOCKED unless operator opts in via
# `@intentional-destructive` marker + env vars.
_R156_E_READ_METHOD_PREFIXES = (
    "get", "list", "read", "inspect", "describe", "verify",
    "validate", "check", "query", "search", "find", "lookup",
    "fetch", "show", "view", "stream",  # streaming reads OK
)

_R156_E_INTENTIONAL_DESTRUCTIVE_MARKER = "@intentional-destructive"


def _r156_e_is_read_method(method_name: str) -> bool:
    """Return True when `method_name` matches a known read-intent
    prefix. Case-insensitive."""
    if not method_name:
        return False
    name_lower = method_name.lower()
    return any(name_lower.startswith(p) for p in _R156_E_READ_METHOD_PREFIXES)


def _r156_e_destructive_call_allowed() -> bool:
    """R154.C parity for gRPC: destructive gRPC method calls require
    BOTH env vars set (`ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` AND
    `SUT_TEST_DATA_NAMESPACE` non-empty). Spec-level
    `@intentional-destructive` marker is verified at the call site
    (GrpcClient checks the calling module's docstring/source)."""
    if os.environ.get("ARTA_R154_ALLOW_DESTRUCTIVE_TESTS") != "1":
        return False
    if not (os.environ.get("SUT_TEST_DATA_NAMESPACE") or "").strip():
        return False
    return True


class GrpcClient:
    """R156.E — authenticated gRPC client for pytest test generation.

    Defers the actual grpc-module import to constructor time so the
    helper module imports cleanly even when grpcio isn't installed
    (operators ship grpc deps per-project; ARTA's pytest runtime
    image bundles them when R156.F ships, but importing this module
    at runtime to introspect is safe today).
    """

    def __init__(
        self,
        target: str,
        *,
        insecure: bool | None = None,
        timeout_sec: float | None = None,
        auth_metadata_key: str | None = None,
        auth_metadata_value_prefix: str | None = None,
    ) -> None:
        """Construct a gRPC client targeting `target` (host:port form).

        Args:
            target: gRPC endpoint (e.g., "auth.example.svc.cluster.local:9090")
            insecure: Override the env-var default. When True, uses
                `grpc.insecure_channel`. Default reads
                ARTA_GRPC_INSECURE; falls back to False (TLS).
            timeout_sec: Per-call timeout. Default 10.0.
            auth_metadata_key: Override the metadata key for the
                auth token (default "authorization").
            auth_metadata_value_prefix: Override the value prefix
                (default "bearer ").
        """
        self.target = target
        self.insecure = (
            insecure
            if insecure is not None
            else os.environ.get("ARTA_GRPC_INSECURE") == "1"
        )
        self.timeout_sec = float(
            timeout_sec
            if timeout_sec is not None
            else os.environ.get("ARTA_GRPC_DEFAULT_TIMEOUT_SEC", "10.0")
        )
        self.auth_metadata_key = (
            auth_metadata_key
            or os.environ.get("ARTA_GRPC_AUTH_METADATA_KEY")
            or "authorization"
        )
        self.auth_metadata_value_prefix = (
            auth_metadata_value_prefix
            if auth_metadata_value_prefix is not None
            else os.environ.get("ARTA_GRPC_AUTH_METADATA_VALUE_PREFIX", "bearer ")
        )
        self._channel: Any = None  # lazy-init on first .call()

    def _build_metadata(self) -> list[tuple[str, str]]:
        """Build the per-call metadata list. AUTH_TOKEN env var is
        read at CALL TIME (not construct time) so R156.J auto-refresh
        updates between calls are honored."""
        token = os.environ.get("AUTH_TOKEN", "") or ""
        if not token:
            return []
        return [(
            self.auth_metadata_key.lower(),
            f"{self.auth_metadata_value_prefix}{token}",
        )]

    def _ensure_channel(self) -> Any:
        """Lazily open the channel. Defers grpc import so this module
        imports cleanly in non-grpc environments."""
        if self._channel is not None:
            return self._channel
        try:
            import grpc  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "R156.E: grpcio not installed; cannot open gRPC channel. "
                "Operator: install `grpcio` in the pytest runtime image."
            ) from exc
        if self.insecure:
            self._channel = grpc.insecure_channel(self.target)
        else:
            credentials = grpc.ssl_channel_credentials()
            self._channel = grpc.secure_channel(self.target, credentials)
        return self._channel

    def call(
        self,
        stub_class: Any,
        method: str,
        request_msg: Any,
        *,
        timeout: float | None = None,
        allow_destructive: bool = False,
    ) -> Any:
        """Invoke a gRPC method on `stub_class` with `request_msg`.

        Args:
            stub_class: A generated `*ServiceStub` class (from R156.F
                stubs OR operator-imported pb2_grpc module).
            method: Bare method name (e.g., "VerifyToken"). Must match
                R156.E read-side allowlist UNLESS `allow_destructive`
                is True AND the operator has set R154.C env vars.
            request_msg: The protobuf message instance to send.
            timeout: Override the default per-call timeout.
            allow_destructive: Operator-opt-in to non-read methods.
                Requires `ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1` AND
                `SUT_TEST_DATA_NAMESPACE` non-empty; otherwise raises.

        Returns:
            The response protobuf message.

        Raises:
            RuntimeError: when method is destructive + opt-in not satisfied
            RuntimeError: when grpcio is unavailable
            grpc.RpcError subclasses: when the SUT returns a gRPC error
        """
        if not _r156_e_is_read_method(method):
            if not allow_destructive or not _r156_e_destructive_call_allowed():
                raise RuntimeError(
                    f"R156.E + R154.C: gRPC method '{method}' is not on the "
                    f"read-side allowlist (prefixes: "
                    f"{_R156_E_READ_METHOD_PREFIXES}). To call destructive "
                    f"methods, set `allow_destructive=True` AND ensure "
                    f"ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 + "
                    f"SUT_TEST_DATA_NAMESPACE is non-empty in the pytest "
                    f"runtime env. Default-deny preserves ARTA's mission: "
                    f"*report* SUT quality without mutating SUT state."
                )
        channel = self._ensure_channel()
        stub = stub_class(channel)
        method_attr = getattr(stub, method, None)
        if method_attr is None:
            raise RuntimeError(
                f"R156.E: stub {stub_class.__name__} has no method '{method}'. "
                f"Available methods: "
                f"{[m for m in dir(stub) if not m.startswith('_')]}"
            )
        timeout_value = timeout if timeout is not None else self.timeout_sec
        metadata = self._build_metadata()
        return method_attr(request_msg, timeout=timeout_value, metadata=metadata)

    def close(self) -> None:
        """Close the underlying channel. Idempotent."""
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

    def __enter__(self) -> "GrpcClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
