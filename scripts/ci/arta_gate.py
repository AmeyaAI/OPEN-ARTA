#!/usr/bin/env python3
"""ARTA CI Stage 7 — Quality Gate Check.

Calls the QualityGateAgent for a build and exits with:
  0 = GO (release approved)
  1 = BLOCK (release blocked)
  2 = WARN (release allowed with warnings)

Usage:
  python scripts/ci/arta_gate.py --build-id 487 [--environment staging]
                                 [--api-url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="ARTA CI — Quality Gate Check")
    parser.add_argument("--build-id", required=True, help="Build ID to check")
    parser.add_argument("--environment", default="staging", help="Target environment")
    parser.add_argument("--api-url", default=os.environ.get("ARTA_API_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("ARTA_API_KEY", ""))
    parser.add_argument("--output", default="", help="File to write gate decision JSON")
    args = parser.parse_args()

    headers = {}
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    print(f"[ARTA-CI] Stage 7: Quality Gate Check")
    print(f"  Build: {args.build_id}")
    print(f"  Environment: {args.environment}")
    print(f"  API: {args.api_url}")

    with httpx.Client(base_url=args.api_url, headers=headers, timeout=60) as client:
        resp = client.post("/api/gates/check", json={
            "build_id": args.build_id,
            "environment": args.environment,
        })
        resp.raise_for_status()
        decision = resp.json()

    gate = decision.get("decision", "BLOCK")
    summary = decision.get("summary", "")
    blocking = decision.get("blocking_checks", [])
    warnings = decision.get("warnings", [])

    print(f"\n  Decision: {gate}")
    print(f"  Summary: {summary}")

    if blocking:
        print(f"  Blocking checks ({len(blocking)}):")
        for c in blocking:
            print(f"    ✗ {c.get('name')}: {c.get('actual')} (expected {c.get('expected')})")

    if warnings:
        print(f"  Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    ⚠ {w.get('name')}: {w.get('actual')} (expected {w.get('expected')})")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(decision, f, indent=2)
        print(f"\n  Decision written to {args.output}")

    if gate == "GO":
        print("\n[ARTA-CI] ✓ RELEASE APPROVED")
        return 0
    elif gate == "WARN":
        print("\n[ARTA-CI] ⚠ RELEASE APPROVED WITH WARNINGS")
        return 2
    else:
        print("\n[ARTA-CI] ✗ RELEASE BLOCKED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
