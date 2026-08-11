"""Phase N1 — generated-test compliance audit.

Detects pre-K/L pattern violations on existing artifacts under
`src/automation/`. The K/L validators in `automation_engineer._validate_response`
only block bad NEW generations; tests written before those validators
landed still carry the bad patterns.

Run modes:
    python tools/audit_existing_tests.py            # human-readable report
    python tools/audit_existing_tests.py --json     # machine-readable
    python tools/audit_existing_tests.py --strict   # exits 1 on any violation (CI guard)

Checks per tool:
    Playwright (K2)  : response.json() must be inside try/catch
    Playwright (L13) : toHaveText('<numeric>') must reference recipe expected_outputs
    Playwright (L15) : >= 2 expect() calls, no hardcoded prod URLs
    Pytest    (L6)   : adversarial tests use assert_adversarial_handled, not bare assert
    Newman           : every item has at least one pm.test() assertion
    k6               : has k6/http import, check import, top-level export default
    ZAP              : has activeScan job
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Violation:
    file: Path
    rule: str
    detail: str


@dataclass
class AuditResult:
    violations: list[Violation] = field(default_factory=list)
    files_scanned: int = 0


def _is_active_spec(p: Path) -> bool:
    name = p.name
    return ".broken" not in name and ".bak" not in name


def audit_playwright_k2(specs_dir: Path) -> list[Violation]:
    """K2 — page.request.* / fetch result calls to response.json() must be
    inside a try/catch, otherwise an HTML response (auth redirect, error
    page) raises `SyntaxError: Unexpected token '<'` and the entire test
    block fails."""
    out: list[Violation] = []
    for f in sorted(specs_dir.glob("req_am_*.spec.ts")):
        if not _is_active_spec(f):
            continue
        text = f.read_text(errors="replace")
        json_calls = len(re.findall(r"response\.json\(\)", text))
        try_blocks = len(re.findall(r"try\s*\{", text))
        if json_calls > 0 and try_blocks < json_calls:
            out.append(Violation(f, "K2",
                f"{json_calls} response.json() calls but only {try_blocks} try-blocks"))
    return out


def audit_playwright_l13(specs_dir: Path) -> list[Violation]:
    """L13 — toHaveText('<numeric>') for analytics specs is a recipe-binding
    smell. Only an issue when the numeric value isn't a static label.
    Heuristic match — flags toHaveText with pure-numeric or pct values."""
    out: list[Violation] = []
    suspect = re.compile(r"toHaveText\(['\"](-?\d+(?:\.\d+)?%?)['\"]\)")
    for f in sorted(specs_dir.glob("req_am_*.spec.ts")):
        if not _is_active_spec(f):
            continue
        text = f.read_text(errors="replace")
        bad = suspect.findall(text)
        if bad:
            out.append(Violation(f, "L13",
                f"hardcoded numeric toHaveText values: {bad[:3]}"))
    return out


def audit_playwright_l15(specs_dir: Path) -> list[Violation]:
    """L15 — quality floor: at least 2 expect() calls and no hardcoded
    https://prod-style URLs (must use baseURL / process.env).

    Phase R3 — a11y specs (`*_a11y.spec.ts`) typically have ONE expect on
    `getViolations()` output (the canonical axe-core pattern). Don't flag
    them under the ≥2 expect() rule. Functional specs still require ≥2.
    """
    out: list[Violation] = []
    for f in sorted(specs_dir.glob("req_am_*.spec.ts")):
        if not _is_active_spec(f):
            continue
        text = f.read_text(errors="replace")
        expects = len(re.findall(r"\bexpect\s*\(", text))
        is_a11y = "_a11y.spec.ts" in f.name
        min_expects = 1 if is_a11y else 2
        if expects < min_expects:
            out.append(Violation(f, "L15", f"only {expects} expect() calls"))
        hard_urls = re.findall(r"https?://(?!\{|\$\{|process\.env)[a-zA-Z0-9.\-]+", text)
        # Allow:
        #   - documentation/example URLs (github, example.com, schema.org)
        #   - localhost in `||` fallback context — `process.env.BASE_URL || 'http://localhost:3000'`
        #     is the canonical dev-fallback pattern and is not a real hardcoded URL
        #   - 127.0.0.1 / 0.0.0.0 (same rationale)
        local_hosts = ("localhost", "127.0.0.1", "0.0.0.0")
        hard_urls = [u for u in hard_urls
                     if "example.com" not in u
                     and "github.com" not in u
                     and "schema.org" not in u
                     and not any(h in u for h in local_hosts)]
        if len(hard_urls) > 1:
            out.append(Violation(f, "L15",
                f"{len(hard_urls)} hardcoded URLs: {hard_urls[:2]}"))
    return out


def audit_pytest_l6(pytest_dir: Path) -> list[Violation]:
    """L6 — adversarial pytest must use the assert_adversarial_handled
    helper. Bare `assert refused or clarification ...` passes against the
    stub default and gives false confidence."""
    out: list[Violation] = []
    bare = re.compile(r"assert\s+.*\brefused\b", re.MULTILINE)
    for f in sorted(pytest_dir.glob("*adversarial*.py")):
        text = f.read_text(errors="replace")
        if "assert_adversarial_handled" in text:
            continue
        if bare.search(text):
            out.append(Violation(f, "L6", "uses bare assert refused-or pattern"))
    return out


def audit_newman(newman_dir: Path) -> list[Violation]:
    """Every Newman item must have at least one pm.test() assertion.
    Also flags hardcoded http(s) literals not using {{base_url}}."""
    out: list[Violation] = []
    if not newman_dir.is_dir():
        return out
    for f in sorted(newman_dir.glob("*.json")):
        try:
            col = json.loads(f.read_text(errors="replace"))
        except Exception as e:
            out.append(Violation(f, "PARSE", f"json error: {e}"))
            continue
        items = col.get("item") or []
        no_assert = 0
        for it in items:
            if isinstance(it, dict):
                events = it.get("event") or []
                has_test = any(
                    "pm.test" in str((ev.get("script") or {}).get("exec", ""))
                    for ev in events if isinstance(ev, dict)
                )
                if not has_test:
                    no_assert += 1
        if no_assert > 0:
            out.append(Violation(f, "NEWMAN",
                f"{no_assert}/{len(items)} items have no pm.test() assertions"))
    return out


def audit_k6(k6_dir: Path) -> list[Violation]:
    """k6 scripts must import http, import check (or use a built-in fail),
    and expose a top-level `export default function`."""
    out: list[Violation] = []
    if not k6_dir.is_dir():
        return out
    for f in sorted(k6_dir.glob("*.js")):
        if f.name.endswith(".broken"):
            continue
        text = f.read_text(errors="replace")
        if "k6/http" not in text:
            out.append(Violation(f, "K6", "missing 'k6/http' import"))
        if "check" not in text and "fail" not in text:
            out.append(Violation(f, "K6", "no check()/fail() — assertions absent"))
        if not re.search(r"^export\s+default\s+function", text, re.MULTILINE):
            out.append(Violation(f, "K6", "missing top-level 'export default function'"))
    return out


def audit_zap(zap_dir: Path) -> list[Violation]:
    """ZAP YAMLs must include an activeScan job (passive-only fails
    auth-coverage requirements)."""
    out: list[Violation] = []
    if not zap_dir.is_dir():
        return out
    try:
        import yaml  # type: ignore
    except ImportError:
        return [Violation(zap_dir, "ZAP", "PyYAML not installed; skipping audit")]
    for f in sorted(zap_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(errors="replace"))
        except Exception as e:
            out.append(Violation(f, "PARSE", f"yaml error: {e}"))
            continue
        jobs = (d or {}).get("jobs") or []
        types = [j.get("type") for j in jobs if isinstance(j, dict)]
        if "activeScan" not in types:
            out.append(Violation(f, "ZAP", f"no activeScan job (jobs={types})"))
    return out


def run_audit() -> AuditResult:
    res = AuditResult()
    pw = ROOT / "src/automation/playwright"
    py = ROOT / "src/automation/python_tests/analytics"
    nm = ROOT / "src/automation/newman"
    k6 = ROOT / "src/automation/k6"
    zap = ROOT / "src/automation/zap"

    if pw.is_dir():
        res.files_scanned += sum(1 for f in pw.glob("req_am_*.spec.ts") if _is_active_spec(f))
        res.violations += audit_playwright_k2(pw)
        res.violations += audit_playwright_l13(pw)
        res.violations += audit_playwright_l15(pw)
    if py.is_dir():
        res.files_scanned += sum(1 for _ in py.glob("*adversarial*.py"))
        res.violations += audit_pytest_l6(py)
    if nm.is_dir():
        res.files_scanned += sum(1 for _ in nm.glob("*.json"))
        res.violations += audit_newman(nm)
    if k6.is_dir():
        res.files_scanned += sum(1 for f in k6.glob("*.js") if not f.name.endswith(".broken"))
        res.violations += audit_k6(k6)
    if zap.is_dir():
        res.files_scanned += sum(1 for _ in zap.glob("*.yaml"))
        res.violations += audit_zap(zap)

    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase N1 — generated-test compliance audit")
    ap.add_argument("--json", action="store_true", help="emit JSON, suitable for CI")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any violation found")
    ap.add_argument("--rules", nargs="*",
                    choices=["K2", "L13", "L15", "L6", "NEWMAN", "K6", "ZAP"],
                    help="restrict to specific rules")
    args = ap.parse_args(argv)

    res = run_audit()
    violations = res.violations
    if args.rules:
        rules = set(args.rules)
        violations = [v for v in violations if v.rule in rules]

    if args.json:
        print(json.dumps({
            "files_scanned": res.files_scanned,
            "violation_count": len(violations),
            "violations": [
                {"file": str(v.file.relative_to(ROOT)), "rule": v.rule, "detail": v.detail}
                for v in violations
            ],
        }, indent=2))
    else:
        print(f"Phase N1 audit — scanned {res.files_scanned} files")
        if not violations:
            print("  OK: no violations found")
        else:
            print(f"  {len(violations)} violation(s):")
            by_rule: dict[str, list[Violation]] = {}
            for v in violations:
                by_rule.setdefault(v.rule, []).append(v)
            for rule, items in sorted(by_rule.items()):
                print(f"\n  [{rule}] ({len(items)} files)")
                for v in items[:10]:
                    print(f"    {v.file.relative_to(ROOT)}: {v.detail}")
                if len(items) > 10:
                    print(f"    ... and {len(items) - 10} more")

    if args.strict and violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
