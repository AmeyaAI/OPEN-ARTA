"""scripts/heal_k6_template_keys.py — one-shot heal for k6 scripts using
template literals as bare object keys (LLM-bias defect).

Mirrors the runtime auto-fix in src/agents/automation_engineer.py
::_validate_k6_script Pass 5c. Verified live in run-638f25:
req_am_020_performance.js line 268 had:

    check(body, {
      `metric matches expected: ${x}`: (b) => ...,
    });

Goja parser fails with `Unexpected token \\``. The fix is to wrap the
template literal in computed-key brackets [...]:

    check(body, {
      [`metric matches expected: ${x}`]: (b) => ...,
    });

Usage (from repo root):
    python3 scripts/heal_k6_template_keys.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

K6_DIR = Path("src/automation/k6")

# Same regex as runtime Pass 5c. Matches a complete `...` template literal
# (with optional ${...} interpolations) immediately followed by `:` (the
# bare-object-key position).
_TEMPLATE_KEY_RE = re.compile(r"(`[^`]*(?:\$\{[^}]*\}[^`]*)*`)\s*:")


def heal_one(path: Path) -> tuple[bool, int]:
    """Return (changed, n_keys_wrapped)."""
    src = path.read_text()
    n = len(_TEMPLATE_KEY_RE.findall(src))
    if n == 0:
        return False, 0
    new_src = _TEMPLATE_KEY_RE.sub(lambda m: f"[{m.group(1)}]:", src)
    if new_src != src:
        path.write_text(new_src)
        return True, n
    return False, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    ap.add_argument("--root", default=str(K6_DIR), help="k6 scripts directory")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    healed: list[tuple[str, int]] = []
    skipped_broken: int = 0
    for path in sorted(root.glob("*.js")):
        if ".broken" in path.name:
            skipped_broken += 1
            continue
        try:
            src = path.read_text()
            n = len(_TEMPLATE_KEY_RE.findall(src))
            if n == 0:
                continue
            if not args.dry_run:
                new_src = _TEMPLATE_KEY_RE.sub(lambda m: f"[{m.group(1)}]:", src)
                path.write_text(new_src)
            healed.append((path.name, n))
            print(f"+ {path.name} ({n} template-literal key(s))")
        except OSError as exc:
            print(f"  ! skip {path.name}: {exc}", file=sys.stderr)
            continue

    mode = "WOULD HEAL" if args.dry_run else "HEALED"
    print(f"\n{mode}: {len(healed)} file(s) (skipped {skipped_broken} broken/quarantined)")
    if healed and args.dry_run:
        print("Run again WITHOUT --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
