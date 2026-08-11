"""Anonymous, fail-silent telemetry client (Tier 1, opt-out).

Design contract (see docs/TELEMETRY.md — the public field list):
- `ARTA_TELEMETRY=0` disables everything: zero network calls, zero DNS lookups.
- Anonymous random install UUID persisted to `.arta/telemetry.json`. No machine
  fingerprinting — the ID is not derived from hostname, MAC, or anything else.
- Events pass the schema allow-list (`schema.validate`) or are dropped.
- Emission is batched and asynchronous; a failure can NEVER block or break a
  test run. After `_CIRCUIT_LIMIT` consecutive flush failures the client stops
  trying for the process lifetime (graceful behavior while the gateway is
  unreachable or not yet deployed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .schema import validate

log = logging.getLogger("arta.telemetry")

ENDPOINT = os.environ.get("ARTA_TELEMETRY_ENDPOINT", "https://telemetry.arta.dev/v1/events")
VERSION = os.environ.get("ARTA_VERSION", "0.1.0")
DOCS_URL = "https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/TELEMETRY.md"

_STATE_FILE = Path(os.environ.get("ARTA_TELEMETRY_STATE", ".arta/telemetry.json"))
_FLUSH_INTERVAL_S = 300
_FLUSH_BATCH = 20
_CIRCUIT_LIMIT = 3
_HEARTBEAT_INTERVAL_S = 7 * 24 * 3600


def enabled() -> bool:
    return os.environ.get("ARTA_TELEMETRY", "1") not in ("0", "false", "no", "off")


def tier2_enabled() -> bool:
    return enabled() and os.environ.get("ARTA_TELEMETRY_EXTENDED") == "1"


class TelemetryClient:
    def __init__(self) -> None:
        self._queue: list[dict] = []
        self._failures = 0
        self._tripped = False
        self._install_id: str | None = None
        self._first_run = False
        self._task: asyncio.Task | None = None
        self._started = datetime.now(timezone.utc)

    # ── identity ────────────────────────────────────────────────────────────
    def install_id(self) -> str:
        if self._install_id:
            return self._install_id
        try:
            if _STATE_FILE.exists():
                state = json.loads(_STATE_FILE.read_text())
                self._install_id = state.get("install_id") or str(uuid.uuid4())
            else:
                self._install_id = str(uuid.uuid4())
                self._first_run = True
                _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _STATE_FILE.write_text(json.dumps({
                    "install_id": self._install_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }))
                try:
                    _STATE_FILE.chmod(0o600)
                except OSError:
                    pass
        except (OSError, json.JSONDecodeError):
            self._install_id = str(uuid.uuid4())  # ephemeral; never blocks
        return self._install_id

    # ── envelope ────────────────────────────────────────────────────────────
    @staticmethod
    def _deploy() -> str:
        if Path("/.dockerenv").exists():
            return "docker"
        return "pip" if os.environ.get("ARTA_INSTALLED_VIA") == "pip" else "source"

    @staticmethod
    def _mode() -> str:
        return "full" if os.environ.get("DATABASE_URL") else "lite"

    def emit(self, event: str, props: dict | None = None) -> None:
        """Queue an event. Never raises; drops anything off-schema."""
        if not enabled() or self._tripped:
            return
        try:
            clean = validate(event, props, tier2_enabled=tier2_enabled())
            if clean is None:
                return
            self._queue.append({
                "install_id": self.install_id(),
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                "version": VERSION,
                "os": platform.system().lower() or "unknown",
                "arch": platform.machine().lower() or "unknown",
                "deploy": self._deploy(),
                "mode": self._mode(),
                "tier": 2 if event not in ("telemetry.opted_out",) and tier2_enabled() else 1,
                "props": clean,
            })
            if len(self._queue) >= _FLUSH_BATCH and self._task is None:
                # Fire-and-forget flush when we are inside a running loop.
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.flush())
                except RuntimeError:
                    pass  # no loop — the periodic task will pick it up
        except Exception:  # noqa: BLE001 — telemetry must never break the host app
            pass

    # ── transport ───────────────────────────────────────────────────────────
    async def flush(self) -> None:
        if not enabled() or self._tripped or not self._queue:
            return
        batch, self._queue = self._queue[:_FLUSH_BATCH * 2], self._queue[_FLUSH_BATCH * 2:]
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.post(ENDPOINT, json={"events": batch})
            if resp.status_code >= 400:
                raise RuntimeError(f"gateway {resp.status_code}")
            self._failures = 0
        except Exception:  # noqa: BLE001 — fail silent, count toward the breaker
            self._failures += 1
            if self._failures >= _CIRCUIT_LIMIT:
                self._tripped = True
                self._queue.clear()
                log.debug("telemetry circuit breaker tripped after %d failures", self._failures)

    async def _periodic(self) -> None:
        heartbeat_due = 0.0
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            heartbeat_due += _FLUSH_INTERVAL_S
            if heartbeat_due >= _HEARTBEAT_INTERVAL_S:
                heartbeat_due = 0.0
                uptime_h = (datetime.now(timezone.utc) - self._started).total_seconds() / 3600
                from .schema import bucket
                self.emit("server.heartbeat", {"mode": self._mode(), "uptime_bucket": bucket(int(uptime_h))})
            await self.flush()

    def start(self) -> None:
        """Called once at API startup: first-run notice + installation event +
        periodic flush/heartbeat task. Safe to call when disabled (no-op)."""
        if not enabled():
            log.info("Telemetry disabled (ARTA_TELEMETRY=0) — no data leaves this machine.")
            return
        self.install_id()
        log.warning(
            "Anonymous usage telemetry is ON (install id %s…). It sends bucketed "
            "counts and enums only — never code, prompts, URLs, or names. "
            "Field list: %s — disable with ARTA_TELEMETRY=0.",
            self.install_id()[:8], DOCS_URL,
        )
        if self._first_run:
            self.emit("installation.created", {"deploy": self._deploy(), "mode": self._mode()})
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._periodic())
        except RuntimeError:
            pass


_client = TelemetryClient()


def emit(event: str, props: dict | None = None) -> None:
    """Module-level convenience used by instrumentation call sites."""
    _client.emit(event, props)


def start() -> None:
    _client.start()
