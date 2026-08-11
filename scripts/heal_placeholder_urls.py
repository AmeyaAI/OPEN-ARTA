"""One-shot heal pass for placeholder-domain URL leaks in generated tests.

Triggered by run-f4fbb5 investigation: the LLM emitted hardcoded
`https://api.example.com/...` URLs in 173 Playwright + 29 k6 + 5 other
specs. Each one fails DNS at runtime with `getaddrinfo ENOTFOUND` and
prevents the test from reaching its assertions.

Heal strategy:
  TS/JS specs (Playwright, Cypress, Selenium):
    'https://api.example.com/v1/users' → `${process.env.API_BASE_URL ??
                                           process.env.BASE_URL ?? ''}/v1/users`
    "https://example.com"              → `${process.env.BASE_URL ?? ''}`
    `https://api.example.com/${id}`    → `${process.env.API_BASE_URL ??
                                           process.env.BASE_URL ?? ''}/${id}`
  k6 (.js):
    'https://api.example.com/x'        → `${__ENV.BASE_URL}/x`
  Newman (.json):
    "https://api.example.com"          → "{{base_url}}"

The validator addition in automation_engineer.py catches future regressions
at gen time. This script repairs files already on disk.

Run from repo root:
    python3 scripts/heal_placeholder_urls.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLACEHOLDER_DOMAINS = (
    "example.com", "example.org", "example.net",
    "acme.com", "acme.app",
    "your-app.com", "your-domain.com", "your-company.com",
    "test.com", "fake.com", "dummy.com",
)


def _placeholder_host_pattern() -> str:
    """Regex char-class fragment matching any of the placeholder hostnames,
    optionally prefixed with `api.` / `www.` / etc. Anchored at scheme."""
    domain_alt = "|".join(re.escape(d) for d in PLACEHOLDER_DOMAINS)
    return rf"https?://(?:[a-zA-Z0-9_-]+\.)*(?:{domain_alt})"


_TS_SINGLE_QUOTED_RE = re.compile(rf"'({_placeholder_host_pattern()})([^']*)'")
_TS_DOUBLE_QUOTED_RE = re.compile(rf'"({_placeholder_host_pattern()})([^"]*)"')
_TS_TEMPLATE_RE = re.compile(
    rf"`({_placeholder_host_pattern()})([^`]*)`"
)


def heal_ts_js(content: str, env_replacement: str = "${process.env.API_BASE_URL ?? process.env.BASE_URL ?? ''}") -> tuple[str, int]:
    """Heal a TS/JS source. Returns (new_content, n_replacements)."""
    n = 0

    def _sub_quoted(match: re.Match) -> str:
        nonlocal n
        n += 1
        # group(1) = scheme://placeholder-host, group(2) = rest of URL/path
        rest = match.group(2)
        return f"`{env_replacement}{rest}`"

    def _sub_template(match: re.Match) -> str:
        nonlocal n
        n += 1
        rest = match.group(2)
        return f"`{env_replacement}{rest}`"

    content = _TS_SINGLE_QUOTED_RE.sub(_sub_quoted, content)
    content = _TS_DOUBLE_QUOTED_RE.sub(_sub_quoted, content)
    content = _TS_TEMPLATE_RE.sub(_sub_template, content)
    return content, n


def heal_k6(content: str) -> tuple[str, int]:
    """k6 uses __ENV.BASE_URL instead of process.env."""
    return heal_ts_js(content, env_replacement="${__ENV.BASE_URL}")


def heal_newman_json(content: str) -> tuple[str, int]:
    """Newman uses Postman variables."""
    pat = re.compile(rf'"({_placeholder_host_pattern()})([^"]*)"')
    n = 0

    def _sub(match: re.Match) -> str:
        nonlocal n
        n += 1
        rest = match.group(2)
        return f'"{{{{base_url}}}}{rest}"'

    return pat.sub(_sub, content), n


def heal_pytest(content: str) -> tuple[str, int]:
    """pytest uses os.environ."""
    pat = re.compile(rf"['\"]({_placeholder_host_pattern()})([^'\"]*)['\"]")
    n = 0

    def _sub(match: re.Match) -> str:
        nonlocal n
        n += 1
        rest = match.group(2)
        return f'f"{{os.environ.get(\\"BASE_URL\\", \\"\\")}}{rest}"'

    return pat.sub(_sub, content), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    ap.add_argument("--root", default="src/automation", help="automation root dir")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    handlers: dict[str, tuple[callable, list[str]]] = {
        "playwright": (heal_ts_js, ["*.spec.ts", "*.spec.js", "*.test.ts"]),
        "cypress":    (heal_ts_js, ["*.cy.ts", "*.cy.js", "*.spec.ts"]),
        "selenium":   (heal_ts_js, ["*.spec.ts", "*.test.ts", "*.js", "*.py"]),
        "k6":         (heal_k6,    ["*.js"]),
        "newman":     (heal_newman_json, ["*.json"]),
        "pytest":     (heal_pytest, ["*.py"]),
        "appium":     (heal_ts_js, ["*.spec.ts", "*.test.ts"]),
    }

    total_files = 0
    total_repls = 0
    for tool, (heal_fn, patterns) in handlers.items():
        tool_dir = root / tool
        if not tool_dir.is_dir():
            continue
        # Selenium spec files are Python; treat as such
        if tool == "selenium":
            for py_file in tool_dir.rglob("*.py"):
                handled, n = heal_pytest(py_file.read_text(errors="ignore"))
                if n:
                    total_files += 1
                    total_repls += n
                    if not args.dry_run:
                        py_file.write_text(handled)
                    print(f"  [{tool}] {py_file.relative_to(root)}: {n} replacements")
        for pat in patterns:
            for file in tool_dir.rglob(pat):
                if "node_modules" in file.parts:
                    continue
                try:
                    src = file.read_text(errors="ignore")
                except OSError:
                    continue
                healed, n = heal_fn(src)
                if n == 0:
                    continue
                total_files += 1
                total_repls += n
                if not args.dry_run:
                    file.write_text(healed)
                print(f"  [{tool}] {file.relative_to(root)}: {n} replacements")

    mode = "WOULD CHANGE" if args.dry_run else "CHANGED"
    print(f"\n{mode}: {total_files} files, {total_repls} URL replacements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
