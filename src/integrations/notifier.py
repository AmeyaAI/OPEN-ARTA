"""ARTA Notification Service — Slack and Teams webhook notifications."""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class NotificationService:
    """Send rich notifications to Slack and/or Microsoft Teams via webhooks.

    Config via env vars: SLACK_WEBHOOK_URL, TEAMS_WEBHOOK_URL.
    """

    def __init__(
        self,
        slack_url: str | None = None,
        teams_url: str | None = None,
    ):
        self.slack_url = slack_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        self.teams_url = teams_url or os.environ.get("TEAMS_WEBHOOK_URL", "")

    @property
    def available(self) -> bool:
        return bool(self.slack_url or self.teams_url)

    async def notify_gate_blocked(self, gate_decision: dict, project: str = "ARTA") -> None:
        """Send alert when a quality gate blocks a release."""
        build_id = gate_decision.get("build_id", "unknown")
        summary = gate_decision.get("summary", "Release blocked")
        blocking = gate_decision.get("blocking_checks", [])

        blocks_text = "\n".join(
            f"  - {c.get('name')}: {c.get('actual')} (expected {c.get('expected')})"
            for c in blocking
        )

        text = (
            f":no_entry: *RELEASE BLOCKED* — Build `{build_id}` ({project})\n"
            f"{summary}\n"
            f"*Blocking checks:*\n{blocks_text}"
        )

        await self._send_slack(text)
        await self._send_teams(
            title=f"Release Blocked — Build {build_id}",
            text=f"{summary}\n\nBlocking checks:\n{blocks_text}",
            color="attention",
        )

    async def notify_p0_defect(self, defect: dict, project: str = "ARTA") -> None:
        """Send alert when a P0 defect is detected."""
        defect_id = defect.get("id", "unknown")
        title = defect.get("title", "")
        root_cause = defect.get("root_cause", "")

        text = (
            f":rotating_light: *P0 DEFECT DETECTED* — `{defect_id}` ({project})\n"
            f"*{title}*\n"
            f"Root cause: {root_cause[:200]}"
        )

        await self._send_slack(text)
        await self._send_teams(
            title=f"P0 Defect — {defect_id}",
            text=f"{title}\n\nRoot cause: {root_cause[:200]}",
            color="attention",
        )

    async def notify_coverage_gap(
        self, requirement_id: str, current_pct: float, threshold_pct: float
    ) -> None:
        """Send alert when a requirement's coverage drops below threshold."""
        text = (
            f":warning: *Coverage Gap* — `{requirement_id}`\n"
            f"Current: {current_pct:.1f}% | Threshold: {threshold_pct:.1f}%"
        )

        await self._send_slack(text)
        await self._send_teams(
            title=f"Coverage Gap — {requirement_id}",
            text=f"Current: {current_pct:.1f}% | Threshold: {threshold_pct:.1f}%",
            color="warning",
        )

    async def notify_run_completed(self, run: dict, project: str = "ARTA") -> None:
        """Send summary when a test run completes."""
        run_id = run.get("run_id", run.get("id", "unknown"))
        passed = run.get("passed", 0)
        failed = run.get("failed", 0)
        total = run.get("total", passed + failed)
        gate = run.get("gate_decision", "N/A")

        emoji = ":white_check_mark:" if gate == "GO" else ":warning:" if gate == "WARN" else ":no_entry:"
        text = (
            f"{emoji} *Run Complete* — `{run_id}` ({project})\n"
            f"Results: {passed}/{total} passed, {failed} failed | Gate: *{gate}*"
        )

        await self._send_slack(text)

    async def notify_auth_stale(
        self,
        project_id: str,
        project_name: str,
        state: str,
        ttl_hours_remaining: float | None,
        hint: str | None = None,
    ) -> None:
        """R75.4 — alert when a project's auth cookie is about to expire.

        Dispatched by the background staleness poller in main.py lifespan
        on state transitions (fresh → stale_soon, stale_soon → expired).
        Operators get advance notice BEFORE the autonomous loop breaks,
        rather than discovering the failure after a 30-minute doomed run.

        Args:
            project_id: project UUID (for log/audit trail)
            project_name: human-readable name shown in the notification
            state: "stale_soon" | "expired" (we never notify on "fresh" or "unknown")
            ttl_hours_remaining: hours until expiry; None for opaque cookies w/o stamp
            hint: pre-built remediation string from the auth-staleness endpoint
        """
        if state == "expired":
            emoji = ":no_entry:"
            severity = "EXPIRED"
            color = "attention"
        elif state == "stale_soon":
            emoji = ":hourglass_flowing_sand:"
            severity = "STALE_SOON"
            color = "warning"
        else:
            # Defensive — should never be called for fresh/unknown
            return

        hours_str = f"~{ttl_hours_remaining:.1f}h" if ttl_hours_remaining is not None else "soon"

        text = (
            f"{emoji} *AUTH {severity}* — Project `{project_name}` "
            f"({project_id[:8]}...) — cookie expires {hours_str}.\n"
            + (f"_{hint}_\n" if hint else "")
            + "Refresh via the *Refresh Auth* modal to keep the autonomous "
            "loop running."
        )

        await self._send_slack(text)
        await self._send_teams(
            title=f"Auth {severity} — {project_name}",
            text=(
                f"Project: {project_name}\n"
                f"State: {state}\n"
                f"TTL remaining: {hours_str}\n"
                + (f"\n{hint}" if hint else "")
            ),
            color=color,
        )

    # ── Transport ────────────────────────────────────────────────────────

    async def _send_slack(self, text: str) -> None:
        if not self.slack_url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.slack_url, json={"text": text})
                if resp.status_code != 200:
                    logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

    async def _send_teams(
        self, title: str, text: str, color: str = "default"
    ) -> None:
        if not self.teams_url:
            return
        try:
            import httpx
            card = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": title, "weight": "bolder", "size": "medium"},
                            {"type": "TextBlock", "text": text, "wrap": True},
                        ],
                    },
                }],
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.teams_url, json=card)
                if resp.status_code != 200:
                    logger.warning("Teams webhook returned %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Teams notification failed: %s", exc)
