"""
ARTA Execution Agent — TEA Layer 5: Test Execution
Orchestrates parallel test execution across Playwright, Newman, and k6.
Parses results, streams live feed, and collects evidence artifacts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import uuid4


class TestStatus(str, Enum):
    PASS  = "PASS"
    FAIL  = "FAIL"
    SKIP  = "SKIP"
    ERROR = "ERROR"


@dataclass
class ExecutionResult:
    test_id: str
    title: str
    status: TestStatus
    tool: str
    duration_ms: int = 0
    error_message: str = ""
    stack_trace: str = ""
    screenshot: str = ""        # S3/local path
    har_file: str = ""          # S3/local path
    logs: str = ""
    # Performance-specific
    p95_ms: float | None = None
    error_rate: float | None = None
    # Security-specific
    security_findings: list[dict] = field(default_factory=list)
    a11y_violations: int = 0
    # Metadata
    requirement_id: str = ""
    priority: str = "P2"
    test_type: str = "UI"
    build_id: str = ""


class ExecutionAgent:
    """
    TEA Execution Agent.

    Dispatches automation scripts to the appropriate runner (Playwright,
    Newman, k6), collects output, and normalizes into ExecutionResult objects.

    Supports real-time streaming of results via async generators for the
    live execution feed in the ARTA dashboard.
    """

    def __init__(
        self,
        event_bus: Any = None,
        base_dir: str = ".",
        results_dir: str = ".arta/results",
        timeout_s: int = 120,
        parallel_workers: int = 4,
    ) -> None:
        self._bus = event_bus
        self._base_dir = Path(base_dir)
        self._results_dir = Path(results_dir)
        self._timeout_s = timeout_s
        self._parallel_workers = parallel_workers
        self._results_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        automation_scripts: dict[str, str],
        build_id: str | None = None,
    ) -> list[dict]:
        """
        Execute all provided automation scripts.

        Args:
            automation_scripts: {filename: content} from AutomationEngineerAgent
            build_id: CI build identifier for result correlation

        Returns: list of ExecutionResult dicts
        """
        build_id = build_id or str(uuid4())[:8]

        # Write scripts to disk
        script_paths = self._write_scripts(automation_scripts)

        # Group by tool
        playwright_scripts = [p for p in script_paths if p.suffix == ".ts" and "playwright" in str(p)]
        newman_scripts     = [p for p in script_paths if p.suffix == ".json" and "newman" in str(p)]
        k6_scripts         = [p for p in script_paths if p.suffix == ".js" and "k6" in str(p)]
        analytics_scripts  = [p for p in script_paths if "analytics" in str(p).lower() and p.suffix == ".py"]

        # Run all tool groups concurrently
        results_groups = await asyncio.gather(
            self._run_playwright_batch(playwright_scripts, build_id),
            self._run_newman_batch(newman_scripts, build_id),
            self._run_k6_batch(k6_scripts, build_id),
            self._run_analytics_batch(analytics_scripts, build_id),
            return_exceptions=True,
        )

        all_results: list[ExecutionResult] = []
        for group in results_groups:
            if isinstance(group, list):
                all_results.extend(group)

        return [self._to_dict(r) for r in all_results]

    async def stream_results(self, run_id: str) -> AsyncGenerator[dict, None]:
        """Server-Sent Events generator for live execution feed."""
        results_file = self._results_dir / f"{run_id}-results.jsonl"
        seen = 0
        while True:
            if results_file.exists():
                lines = results_file.read_text().splitlines()
                for line in lines[seen:]:
                    if line.strip():
                        yield json.loads(line)
                        seen += 1
            await asyncio.sleep(0.5)

    # ── Playwright ───────────────────────────────────────────────────────────

    async def _run_playwright_batch(
        self,
        scripts: list[Path],
        build_id: str,
    ) -> list[ExecutionResult]:
        if not scripts:
            return []
        results: list[ExecutionResult] = []
        report_file = self._results_dir / f"{build_id}-playwright-junit.xml"

        # Run all Playwright specs together
        spec_args = " ".join(str(s) for s in scripts)
        cmd = (
            f"npx playwright test {spec_args} "
            f"--reporter=junit "
            f"--output={self._results_dir}/{build_id}-pw-artifacts"
        )
        output, returncode = await self._run_subprocess(cmd)

        # Parse JUnit XML if it was produced
        if report_file.exists():
            results = self._parse_junit_xml(report_file, "playwright", build_id)
        elif returncode != 0:
            # Playwright didn't produce XML — parse stdout
            results = self._parse_playwright_stdout(output, build_id)
        else:
            results = self._parse_playwright_stdout(output, build_id)

        return results

    def _parse_playwright_stdout(self, output: str, build_id: str) -> list[ExecutionResult]:
        """Parse Playwright console output when JUnit XML is not available."""
        results: list[ExecutionResult] = []
        for line in output.splitlines():
            # Playwright output patterns: ✓ test title (Xms) / ✗ test title
            pass_match = re.search(r"[✓✔]\s+(.+?)\s+\((\d+)m?s?\)", line)
            fail_match = re.search(r"[✗✘×]\s+(.+)", line)
            if pass_match:
                results.append(ExecutionResult(
                    test_id=self._title_to_id(pass_match.group(1)),
                    title=pass_match.group(1),
                    status=TestStatus.PASS,
                    tool="playwright",
                    duration_ms=int(pass_match.group(2)),
                    build_id=build_id,
                ))
            elif fail_match:
                results.append(ExecutionResult(
                    test_id=self._title_to_id(fail_match.group(1)),
                    title=fail_match.group(1),
                    status=TestStatus.FAIL,
                    tool="playwright",
                    build_id=build_id,
                ))
        return results

    # ── Newman ───────────────────────────────────────────────────────────────

    async def _run_newman_batch(
        self,
        collections: list[Path],
        build_id: str,
    ) -> list[ExecutionResult]:
        if not collections:
            return []
        results: list[ExecutionResult] = []
        for collection in collections:
            report_file = self._results_dir / f"{build_id}-newman-junit.xml"
            cmd = (
                f"newman run {collection} "
                f"--reporters junit "
                f"--reporter-junit-export {report_file} "
                f"--timeout-request 10000"
            )
            _, _ = await self._run_subprocess(cmd)
            if report_file.exists():
                results.extend(self._parse_junit_xml(report_file, "newman", build_id))
        return results

    # ── k6 ───────────────────────────────────────────────────────────────────

    async def _run_k6_batch(
        self,
        scripts: list[Path],
        build_id: str,
    ) -> list[ExecutionResult]:
        if not scripts:
            return []
        results: list[ExecutionResult] = []
        for script in scripts:
            json_out = self._results_dir / f"{build_id}-k6-results.json"
            summary_out = self._results_dir / f"{build_id}-k6-summary.json"
            # Prefer TARGET_BASE_URL (set by execution router) then BASE_URL then default
            base_url = (
                os.environ.get("TARGET_BASE_URL")
                or os.environ.get("BASE_URL", "http://localhost:3000")
            )
            cmd = (
                f"k6 run {script} "
                f"--out json={json_out} "
                f"--summary-export={summary_out}"
            )
            output, returncode = await self._run_subprocess(
                cmd,
                env={**os.environ, "BASE_URL": base_url, "TARGET_BASE_URL": base_url},
            )
            results.extend(self._parse_k6_results(summary_out, json_out, output, returncode, build_id))
        return results

    def _parse_k6_results(
        self,
        summary_file: Path,
        json_file: Path,
        stdout: str,
        returncode: int,
        build_id: str,
    ) -> list[ExecutionResult]:
        """Parse k6 summary export into ExecutionResult."""
        p95 = None
        error_rate = None

        # Primary: use --summary-export JSON which has aggregated p95
        if summary_file.exists():
            try:
                summary = json.loads(summary_file.read_text())
                p95 = (
                    summary.get("metrics", {})
                    .get("http_req_duration", {})
                    .get("values", {})
                    .get("p(95)")
                )
                error_rate = (
                    summary.get("metrics", {})
                    .get("http_req_failed", {})
                    .get("values", {})
                    .get("rate")
                )
            except (json.JSONDecodeError, AttributeError):
                pass

        # k6 exits 0 on pass, non-zero on threshold failure
        status = TestStatus.PASS if returncode == 0 else TestStatus.FAIL
        title = f"k6 Performance · {json_file.stem}"
        test_id = f"TC-PERF-{build_id}"

        # Phase J9: emit one step per declared k6 group + per-endpoint metric.
        # k6's summary JSON carries `metrics.http_req_duration{group, status}`
        # tags when scripts use `tags: { step: '00' }` (chain_aware_k6 does).
        # We surface those as steps so the timeline reflects per-request data.
        try:
            from ..api.routers.execution import record_step
            if summary_file.exists():
                summary = json.loads(summary_file.read_text())
                # Walk per-tag breakdown when `summaryTrendStats` was captured
                # by group/step. k6 doesn't emit per-request rows by default,
                # so we use the aggregated p95 as the duration_ms proxy.
                metrics = (summary.get("metrics") or {}).get("http_req_duration", {})
                values = metrics.get("values") or {}
                if values:
                    record_step(
                        build_id,
                        test_id=test_id,
                        seq=0,
                        method="PERF",
                        path=json_file.stem,
                        status=200 if returncode == 0 else 500,
                        duration_ms=int(values.get("p(95)") or values.get("avg") or 0),
                        error=None if returncode == 0 else f"k6 thresholds breached (rc={returncode})",
                        cascade_skip=False,
                        cascade_reason=None,
                        provider_contract_violation=False,
                    )
        except Exception:
            pass   # never let step recording break the parser

        return [ExecutionResult(
            test_id=test_id,
            title=title,
            status=status,
            tool="k6",
            p95_ms=p95,
            error_rate=error_rate,
            logs=stdout[-2000:] if stdout else "",
            build_id=build_id,
            test_type="Performance",
        )]

    # ── Analytics ─────────────────────────────────────────────────────────────

    async def _run_analytics_batch(
        self,
        scripts: list[Path],
        build_id: str,
    ) -> list[ExecutionResult]:
        """Execute analytics validation scripts (Python-based).

        Analytics scripts use tolerance-based assertions for numerical values,
        LLM-judge for narrative grounding, and adversarial scenario handling.
        Each script should output a JSON summary to stdout with structure:
          {"tests": [{"name": ..., "status": "pass"|"fail", "duration_ms": ..., "details": ...}]}
        """
        if not scripts:
            return []
        results: list[ExecutionResult] = []

        for script in scripts:
            cmd = f"python {script} --output json"
            output, returncode = await self._run_subprocess(cmd)

            # Try to parse structured JSON output
            parsed = False
            try:
                data = json.loads(output.strip().splitlines()[-1])  # Last line is JSON
                for t in data.get("tests", []):
                    status = TestStatus.PASS if t.get("status") == "pass" else TestStatus.FAIL
                    results.append(ExecutionResult(
                        test_id=self._title_to_id(t.get("name", script.stem)),
                        title=t.get("name", script.stem),
                        status=status,
                        tool="analytics",
                        duration_ms=t.get("duration_ms", 0),
                        error_message=t.get("details", "") if status == TestStatus.FAIL else "",
                        build_id=build_id,
                        test_type="Analytics",
                    ))
                parsed = True
            except (json.JSONDecodeError, IndexError):
                pass

            if not parsed:
                # Fallback: treat entire script as one test
                results.append(ExecutionResult(
                    test_id=self._title_to_id(script.stem),
                    title=f"Analytics · {script.stem}",
                    status=TestStatus.PASS if returncode == 0 else TestStatus.FAIL,
                    tool="analytics",
                    error_message=output[-500:] if returncode != 0 else "",
                    logs=output[-2000:],
                    build_id=build_id,
                    test_type="Analytics",
                ))

        return results

    # ── JUnit XML Parser ──────────────────────────────────────────────────────

    def _parse_junit_xml(
        self,
        xml_file: Path,
        tool: str,
        build_id: str,
    ) -> list[ExecutionResult]:
        """Parse JUnit XML into ExecutionResult objects."""
        results: list[ExecutionResult] = []
        try:
            root = ET.parse(xml_file).getroot()
            # iter("testcase") handles nested <testsuites>/<testsuite>/<testcase>
            for case in root.iter("testcase"):
                name = case.get("name", "")
                duration_ms = int(float(case.get("time", "0")) * 1000)
                failure = case.find("failure")
                error   = case.find("error")
                skipped = case.find("skipped")

                if skipped is not None:
                    status = TestStatus.SKIP
                    msg = ""
                elif failure is not None:
                    status = TestStatus.FAIL
                    msg = failure.get("message", failure.text or "")
                elif error is not None:
                    status = TestStatus.ERROR
                    msg = error.get("message", error.text or "")
                else:
                    status = TestStatus.PASS
                    msg = ""

                results.append(ExecutionResult(
                    test_id=self._title_to_id(name),
                    title=name,
                    status=status,
                    tool=tool,
                    duration_ms=duration_ms,
                    error_message=msg,
                    build_id=build_id,
                ))
        except ET.ParseError:
            pass
        return results

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def _run_subprocess(
        self,
        cmd: str,
        env: dict | None = None,
        cwd: str | None = None,
    ) -> tuple[str, int]:
        """Run a shell command asynchronously with timeout."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env or os.environ.copy(),
                cwd=cwd or str(self._base_dir),
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout_s,
            )
            return stdout.decode("utf-8", errors="replace"), proc.returncode or 0
        except asyncio.TimeoutError:
            # F6-13: kill + reap on timeout. Without this, the subprocess keeps
            # running after we return, holding file handles + memory + a
            # zombie PID until the container restarts.
            if proc is not None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass  # already gone or refusing to die — best effort
                except Exception:
                    pass
            return f"Command timed out after {self._timeout_s}s: {cmd}", 1
        except Exception as exc:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                except Exception:
                    pass
            return f"Execution error: {exc}", 1

    def _write_scripts(self, scripts: dict[str, str]) -> list[Path]:
        """Write script content to disk, return paths."""
        paths: list[Path] = []
        for filename, content in scripts.items():
            path = self._base_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            paths.append(path)
        return paths

    def _title_to_id(self, title: str) -> str:
        """Convert test title to a TC-xxx style ID."""
        # Extract existing TC-xxx if present
        match = re.search(r"TC-\d+", title)
        if match:
            return match.group(0)
        # Generate deterministic ID from title hash
        return f"TC-{abs(hash(title)) % 9000 + 1000}"

    def _to_dict(self, r: ExecutionResult) -> dict:
        return {
            "test_id": r.test_id,
            "title": r.title,
            "status": r.status.value,
            "tool": r.tool,
            "duration_ms": r.duration_ms,
            "error_message": r.error_message,
            "stack_trace": r.stack_trace,
            "screenshot": r.screenshot,
            "har_file": r.har_file,
            "logs": r.logs,
            "p95_ms": r.p95_ms,
            "error_rate": r.error_rate,
            "security_findings": r.security_findings,
            "a11y_violations": r.a11y_violations,
            "requirement_id": r.requirement_id,
            "priority": r.priority,
            "test_type": r.test_type,
            "build_id": r.build_id,
        }
