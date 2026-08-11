"""scripts/heal_k6_durations.py — one-shot heal for k6 scripts whose
cumulative `duration:` time exceeds the 60s budget.

Mirrors the runtime auto-fix in src/agents/automation_engineer.py
::_validate_k6_script Pass 7 (Fix W). Verified live in run-6d6274:
req_am_*_performance.js had stages summing to 165s/70s/65s — k6 kills
scripts at 200s, so over-long scripts produce timeouts not perf signal.

The fix proportionally scales every `duration: '<n><unit>'` literal so
the new cumulative total is ~55s (5s headroom under 60s budget).

Usage (from repo root):
    python3 scripts/heal_k6_durations.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

K6_DIR = Path("src/automation/k6")

_DURATION_RE = re.compile(r"duration\s*:\s*['\"]([^'\"]+)['\"]")


def _to_seconds(spec: str) -> float:
    spec = spec.strip().strip("'\"")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(s|m|ms)?$", spec)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return n * 60 if unit == "m" else n / 1000 if unit == "ms" else n


def heal_one(path: Path, dry_run: bool = False) -> tuple[bool, float, float, int]:
    """Return (changed, original_total_s, new_total_s, n_durations)."""
    src = path.read_text()
    durations = _DURATION_RE.findall(src)
    if not durations:
        return False, 0.0, 0.0, 0
    total = sum(_to_seconds(d) for d in durations)
    if total <= 60:
        return False, total, total, len(durations)

    scale = 55.0 / total

    def _trim(m: re.Match) -> str:
        spec = m.group(1)
        new_seconds = max(1, round(_to_seconds(spec) * scale))
        return f"duration: '{new_seconds}s'"

    new_src = _DURATION_RE.sub(_trim, src)
    new_total = sum(
        _to_seconds(d) for d in _DURATION_RE.findall(new_src)
    )

    if not dry_run and new_src != src:
        path.write_text(new_src)
    return True, total, new_total, len(durations)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    ap.add_argument("--root", default=str(K6_DIR), help="k6 scripts directory")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    healed = 0
    skipped_broken = 0
    for path in sorted(root.glob("*.js")):
        if ".broken" in path.name:
            skipped_broken += 1
            continue
        try:
            changed, original, new, n = heal_one(path, dry_run=args.dry_run)
            if changed:
                healed += 1
                action = "WOULD HEAL" if args.dry_run else "HEALED"
                print(f"{action} {path.name}: {original:.0f}s → {new:.0f}s ({n} duration literals)")
        except OSError as exc:
            print(f"  ! skip {path.name}: {exc}", file=sys.stderr)

    mode = "WOULD HEAL" if args.dry_run else "HEALED"
    print(f"\n{mode}: {healed} file(s) (skipped {skipped_broken} broken/quarantined)")
    if healed and args.dry_run:
        print("Run again WITHOUT --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
