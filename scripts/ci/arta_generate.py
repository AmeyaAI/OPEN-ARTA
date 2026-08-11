#!/usr/bin/env python3
"""ARTA CI Stage 2 — AI Test Generation.

Runs the TEA pipeline (RequirementIntel → Strategy → ATDDDesigner)
for changed requirements and outputs generated tests + manifest.json.

Usage:
  python scripts/ci/arta_generate.py [--requirement-ids REQ-001,REQ-002]
                                     [--output-dir .arta/generated]
                                     [--api-url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="ARTA CI — AI Test Generation")
    parser.add_argument("--requirement-ids", default="", help="Comma-separated requirement IDs to generate for")
    parser.add_argument("--output-dir", default=".arta/generated", help="Output directory for generated tests")
    parser.add_argument("--api-url", default=os.environ.get("ARTA_API_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("ARTA_API_KEY", ""))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    headers = {}
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    req_ids = [r.strip() for r in args.requirement_ids.split(",") if r.strip()]

    print(f"[ARTA-CI] Stage 2: AI Test Generation")
    print(f"  API: {args.api_url}")
    print(f"  Requirements: {req_ids or 'all pending'}")
    print(f"  Output: {output_dir}")

    manifest = {"stage": "generate", "requirement_ids": req_ids, "tests": []}

    with httpx.Client(base_url=args.api_url, headers=headers, timeout=120) as client:
        # 1. Fetch requirements to generate for
        if not req_ids:
            resp = client.get("/api/requirements")
            resp.raise_for_status()
            reqs = resp.json().get("requirements", [])
            req_ids = [r["id"] for r in reqs[:10]]  # Cap at 10 for CI

        # 2. For each requirement, trigger test generation
        for req_id in req_ids:
            print(f"  Generating tests for {req_id}...")
            try:
                resp = client.post("/api/tests/generate", json={
                    "requirement_ids": [req_id],
                    "tools": ["playwright", "newman", "k6"],
                })
                resp.raise_for_status()
                result = resp.json()

                tests = result.get("tests", result.get("scenarios", []))
                for t in tests:
                    manifest["tests"].append({
                        "test_id": t.get("test_id", t.get("id", "")),
                        "requirement_id": req_id,
                        "title": t.get("title", ""),
                        "tool": t.get("tool", "playwright"),
                    })
                print(f"    → {len(tests)} tests generated")
            except httpx.HTTPStatusError as exc:
                print(f"    ⚠ Generation failed for {req_id}: {exc.response.status_code}")
            except Exception as exc:
                print(f"    ⚠ Generation error for {req_id}: {exc}")

    # 3. Write manifest
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\n[ARTA-CI] Manifest written to {manifest_path}")
    print(f"[ARTA-CI] Total tests generated: {len(manifest['tests'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
