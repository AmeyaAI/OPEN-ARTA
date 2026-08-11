"""Telemetry client contract tests (open-core trust guarantees).

These tests enforce the promises documented in docs/TELEMETRY.md:
schema allow-listing, count bucketing, ZERO egress when disabled, and the
circuit breaker that silences the client after repeated gateway failures.
"""
from __future__ import annotations

import socket

import pytest

from src.telemetry.schema import TIER1_EVENTS, TIER2_EVENTS, bucket, validate


# ── schema allow-list ────────────────────────────────────────────────────────

def test_unknown_event_dropped():
    assert validate("totally.unknown", {"x": "y"}) is None


def test_unknown_props_dropped():
    clean = validate("test.generated", {"runtime": "newman", "sut_url": "https://secret.example.com"})
    assert clean == {"runtime": "newman"}


def test_out_of_enum_value_dropped():
    clean = validate("test.generated", {"runtime": "not-a-runtime", "count_bucket": "1-9"})
    assert clean == {"count_bucket": "1-9"}


def test_tier2_gated():
    assert validate("gate.evaluated", {"decision": "pass"}) is None
    assert validate("gate.evaluated", {"decision": "pass"}, tier2_enabled=True) == {"decision": "pass"}


def test_no_free_text_props_in_schema():
    # Every allowed value set must be a closed enum — no prop may accept arbitrary strings.
    for schema in list(TIER1_EVENTS.values()) + list(TIER2_EVENTS.values()):
        for allowed in schema.values():
            assert isinstance(allowed, set) and allowed, "schema props must be closed enums"


# ── bucketing ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n,expected", [
    (-1, "0"), (0, "0"), (1, "1-9"), (9, "1-9"), (10, "10-49"),
    (49, "10-49"), (50, "50-199"), (199, "50-199"), (200, "200+"), (10_000, "200+"),
])
def test_bucket_edges(n, expected):
    assert bucket(n) == expected


# ── zero egress when disabled ────────────────────────────────────────────────

def test_no_egress_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTA_TELEMETRY", "0")
    monkeypatch.setenv("ARTA_TELEMETRY_STATE", str(tmp_path / "t.json"))

    def _boom(*args, **kwargs):  # any socket construction = test failure
        raise AssertionError("network attempted while telemetry disabled")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    from src.telemetry.client import TelemetryClient
    c = TelemetryClient()
    c.emit("installation.created", {"deploy": "source", "mode": "lite"})
    c.start()
    assert c._queue == []  # disabled emit queues nothing
    # state file must not even be created when disabled
    assert not (tmp_path / "t.json").exists()


# ── circuit breaker ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTA_TELEMETRY", "1")
    monkeypatch.setenv("ARTA_TELEMETRY_STATE", str(tmp_path / "t.json"))
    import src.telemetry.client as tc
    monkeypatch.setattr(tc, "_STATE_FILE", tmp_path / "t.json")

    c = tc.TelemetryClient()

    class _FailingClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise ConnectionError("gateway down")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    for i in range(tc._CIRCUIT_LIMIT):
        c.emit("project.created", {"count_bucket": "1-9"})
        await c.flush()
    assert c._tripped is True
    c.emit("project.created", {"count_bucket": "1-9"})
    assert c._queue == []  # tripped client queues nothing


def test_install_id_is_random_uuid(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTA_TELEMETRY", "1")
    import src.telemetry.client as tc
    monkeypatch.setattr(tc, "_STATE_FILE", tmp_path / "t.json")
    c = tc.TelemetryClient()
    iid = c.install_id()
    import uuid
    assert uuid.UUID(iid).version == 4  # random, not derived from the machine
    # stable across a second client (persisted)
    c2 = tc.TelemetryClient()
    assert c2.install_id() == iid
