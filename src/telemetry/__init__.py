"""ARTA anonymous telemetry (open core — see docs/TELEMETRY.md).

Public surface: `emit(event, props)`, `start()`, `bucket(n)`, `enabled()`.
Everything is fail-silent and schema-allow-listed; `ARTA_TELEMETRY=0` produces
zero network activity.
"""
from .client import emit, enabled, start, tier2_enabled  # noqa: F401
from .schema import bucket  # noqa: F401
