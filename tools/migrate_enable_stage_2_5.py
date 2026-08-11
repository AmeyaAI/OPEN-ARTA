#!/usr/bin/env python3
"""Phase R6 — opt existing projects into K1 Stage 2.5 auto-discovery.

Flips `discovery_settings.stage_2_5_enabled=True` and
`discovery_settings.discovery_pending=True` for projects where
`is_api_only=False` AND the flag is not already True.

Skips api-only projects (no UI surface to drive Playwright HAR capture)
and projects already opted in (idempotent — safe to re-run).

The .arta/projects.json shape is a top-level dict keyed by project_id;
no `.projects` wrapper.

Usage:
    python3 tools/migrate_enable_stage_2_5.py            # dry-run
    python3 tools/migrate_enable_stage_2_5.py --apply    # writes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTS_JSON = ROOT / ".arta" / "projects.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run)")
    args = ap.parse_args(argv)

    if not PROJECTS_JSON.is_file():
        print(f"FAIL: {PROJECTS_JSON} not found")
        return 1

    raw = PROJECTS_JSON.read_text()
    data = json.loads(raw)

    flipped: list[str] = []
    skipped_api_only: list[str] = []
    skipped_already: list[str] = []

    for pid, project in data.items():
        if not isinstance(project, dict):
            continue
        if project.get("is_api_only"):
            skipped_api_only.append(f"{pid} ({project.get('name', '?')})")
            continue
        settings = project.setdefault("discovery_settings", {})
        if settings.get("stage_2_5_enabled"):
            skipped_already.append(f"{pid} ({project.get('name', '?')})")
            continue
        settings["stage_2_5_enabled"] = True
        settings["discovery_pending"] = True
        flipped.append(f"{pid} ({project.get('name', '?')})")

    print(f"Phase R6 migration — projects scanned: {len(data)}")
    print(f"  Will flip:            {len(flipped)}")
    for p in flipped:
        print(f"    + {p}")
    print(f"  Skipped (api-only):   {len(skipped_api_only)}")
    for p in skipped_api_only:
        print(f"    - {p}")
    print(f"  Skipped (already on): {len(skipped_already)}")
    for p in skipped_already:
        print(f"    - {p}")

    if not flipped:
        print("\nNothing to do.")
        return 0

    if not args.apply:
        print("\n(dry-run — re-run with --apply to write)")
        return 0

    backup = PROJECTS_JSON.with_suffix(".json.pre-R6.bak")
    backup.write_text(raw)
    PROJECTS_JSON.write_text(json.dumps(data, indent=2))
    print(f"\nWrote {PROJECTS_JSON}")
    print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
