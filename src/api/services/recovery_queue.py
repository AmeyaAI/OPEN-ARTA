"""R125.J — circuit-breaker recovery auto-queue.

When an LLM circuit breaker transitions HALF_OPEN → CLOSED (auth restored,
rate-limit recovered, network healed), walk GENERATED_TESTS for tests that
were marked `generation_source=failed` during the OPEN window and emit
regen markers into `.arta/regen_queue/`. The R42.6 consumer drains them.

Per the user's standing directive "increase pass rate", this closes the
orphaned-requirements class seen after an LLM-provider 401 incident: the
affected requirements stayed broken indefinitely until the operator
manually re-triggered each. Post-R125.J, recovery is automatic.

R30.3 still gates: defects with triage_category in
{operator_review, sut_regression} are NOT auto-healed. Only test_gen_bug
class (the failed-during-OPEN-window) gets the auto-rebuild marker.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

QUEUE_DIR = Path(".arta/regen_queue")

# Window during which we consider a test "failed during the OPEN window"
# and eligible for auto-rebuild. Conservative: 24h after the failed_at
# timestamp. Older failures may have other root causes; let the operator
# manually re-trigger.
_RECOVERY_WINDOW_SECS = 24 * 60 * 60


def on_circuit_recovered(circuit_name: str) -> None:
    """R125.J — invoked from CircuitBreaker.call() on HALF_OPEN → CLOSED.

    Walks GENERATED_TESTS for tests with `generation_source == "failed"`
    AND a fresh-enough `_gen_metrics.failed_at` timestamp. For each match,
    writes a regen marker to `.arta/regen_queue/<test_id>.json`. R42.6
    consumer drains them in the next cycle.

    Best-effort: a failure here must NEVER block circuit-recovery itself.
    The caller wraps in try/except + log.debug.
    """
    try:
        # Lazy import — GENERATED_TESTS is a runtime-mutable module global
        from ..routers.tests import GENERATED_TESTS
    except Exception as exc:
        log.warning("R125.J: cannot import GENERATED_TESTS: %s", exc)
        return

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    queued = 0
    skipped_not_failed = 0
    skipped_stale = 0
    skipped_already_queued = 0
    now = time.time()

    for t in list(GENERATED_TESTS):
        gen_source = t.get("generation_source")
        if gen_source != "failed":
            skipped_not_failed += 1
            continue

        # Within the recovery window? Use _gen_metrics.failed_at if present;
        # else fall back to t["generated_at"]; else assume fresh
        failed_at_str = None
        gm = t.get("_gen_metrics") or {}
        if isinstance(gm, dict):
            failed_at_str = gm.get("failed_at")
        if not failed_at_str:
            failed_at_str = t.get("generated_at")

        if failed_at_str:
            try:
                failed_dt = datetime.fromisoformat(failed_at_str.replace("Z", "+00:00"))
                age_secs = now - failed_dt.timestamp()
                if age_secs > _RECOVERY_WINDOW_SECS:
                    skipped_stale += 1
                    continue
            except Exception:
                # Unparseable timestamp — treat as fresh
                pass

        test_id = t.get("id") or t.get("test_id")
        if not test_id:
            continue

        marker_path = QUEUE_DIR / f"{test_id}.json"
        if marker_path.exists():
            skipped_already_queued += 1
            continue

        marker = {
            "test_id": test_id,
            "requirement_id": t.get("requirement_id"),
            "triage_category": "test_gen_bug",  # R30.3-gated; OK to auto-heal
            "status_code": None,
            "signals": ["r125_j_circuit_recovery", f"circuit:{circuit_name}"],
            "sample_error": str(t.get("generation_failure") or "")[:300],
            "queued_by": "R125.J_circuit_recovery",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            marker_path.write_text(json.dumps(marker, indent=2))
            queued += 1
        except Exception as write_exc:
            log.warning(
                "R125.J: failed to write marker for %s: %s",
                test_id, write_exc,
            )

    log.info(
        "R125.J: circuit '%s' recovered → queued %d regen markers "
        "(skipped %d non-failed, %d stale, %d already-queued)",
        circuit_name, queued, skipped_not_failed, skipped_stale,
        skipped_already_queued,
    )
