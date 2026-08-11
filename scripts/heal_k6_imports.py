"""scripts/heal_k6_imports.py — one-shot heal for k6 scripts missing
the 'k6/http' import.

Mirrors the runtime auto-inject in
src/agents/automation_engineer.py::_validate_k6_script Pass 0.5 — repairs
on-disk drift for scripts generated before the validator landed.

Verified live in run-170e18: 9 k6 scripts on disk lacked the import,
producing `ReferenceError: http is not defined` at runtime. After this
heal, future regenerations get the import via the runtime validator;
existing scripts get it via this one-shot.

Usage (from repo root):
    python3 scripts/heal_k6_imports.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

K6_DIR = Path("src/automation/k6")

_USES_HTTP_RE = re.compile(
    r"\bhttp\.(get|post|put|patch|del|delete|batch|request|head|options|asyncRequest)\b",
)
_HAS_HTTP_IMPORT_RE = re.compile(
    r"^\s*import\s+http\s+from\s+['\"]k6/http['\"]",
    re.MULTILINE,
)
_USES_CHECK_RE = re.compile(r"\bcheck\s*\(")
_USES_SLEEP_RE = re.compile(r"\bsleep\s*\(")
_HAS_K6_NAMED_IMPORT_RE = re.compile(
    r"^\s*import\s*\{\s*([^}]+?)\s*\}\s*from\s*['\"]k6['\"]",
    re.MULTILINE,
)


def heal_one(path: Path) -> tuple[bool, list[str]]:
    """Return (changed, summary_lines) for one k6 file."""
    src = path.read_text()
    changes: list[str] = []
    new_src = src

    if _USES_HTTP_RE.search(new_src) and not _HAS_HTTP_IMPORT_RE.search(new_src):
        new_src = "import http from 'k6/http';\n" + new_src
        changes.append("+ import http from 'k6/http'")

    if not _HAS_K6_NAMED_IMPORT_RE.search(new_src):
        wanted: list[str] = []
        if _USES_CHECK_RE.search(new_src):
            wanted.append("check")
        if _USES_SLEEP_RE.search(new_src):
            wanted.append("sleep")
        if wanted:
            new_src = "import { " + ", ".join(wanted) + " } from 'k6';\n" + new_src
            changes.append("+ import { " + ", ".join(wanted) + " } from 'k6'")

    if new_src != src:
        return True, changes
    return False, []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    ap.add_argument("--root", default=str(K6_DIR), help="k6 scripts directory")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    healed: list[str] = []
    skipped: int = 0
    for path in sorted(root.glob("*.js")):
        # Skip already-quarantined files
        if ".broken-" in path.name or path.name.endswith(".broken"):
            skipped += 1
            continue
        try:
            changed, changes = heal_one(path)
        except OSError as exc:
            print(f"  ! skip {path.name}: {exc}", file=sys.stderr)
            continue
        if changed:
            healed.append(path.name)
            print(f"+ {path.relative_to(root.parent)}")
            for c in changes:
                print(f"    {c}")
            if not args.dry_run:
                # Re-read in case we computed against in-memory copy
                src = path.read_text()
                heal_changed, _ = heal_one(path)
                # Apply the heal
                new_src = src
                if _USES_HTTP_RE.search(new_src) and not _HAS_HTTP_IMPORT_RE.search(new_src):
                    new_src = "import http from 'k6/http';\n" + new_src
                if not _HAS_K6_NAMED_IMPORT_RE.search(new_src):
                    wanted: list[str] = []
                    if _USES_CHECK_RE.search(new_src):
                        wanted.append("check")
                    if _USES_SLEEP_RE.search(new_src):
                        wanted.append("sleep")
                    if wanted:
                        new_src = "import { " + ", ".join(wanted) + " } from 'k6';\n" + new_src
                path.write_text(new_src)

    mode = "WOULD HEAL" if args.dry_run else "HEALED"
    print(f"\n{mode}: {len(healed)} file(s) (skipped {skipped} broken/quarantined)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
