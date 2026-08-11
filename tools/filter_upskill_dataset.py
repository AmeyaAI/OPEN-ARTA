"""Phase O — pre-training quality filter for upskill datasets.

Reads JSONL files from `data/upskill/`, drops records with teacher_score
below a threshold, and emits a clean JSONL ready for `scripts/train_upskill.py`.

Why this exists: low-score teacher critiques (score < 4) often reflect
that the student's draft was so bad the teacher couldn't fully fix it,
producing a partial/wrong "improved_gherkin" output. Training on those
pairs teaches the model to emit partial fixes — counterproductive.

Run:
    # Dry-run — report stats only
    python tools/filter_upskill_dataset.py

    # Filter and write a curated dataset
    python tools/filter_upskill_dataset.py --apply --min-score 5

    # Filter a specific input file
    python tools/filter_upskill_dataset.py --apply --input data/upskill/upskill_dataset_corpus-a1b2c3d4_*.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_GLOB = "data/upskill/*.jsonl"


def load_records(input_glob: str) -> list[tuple[Path, dict]]:
    """Returns [(file_path, record), ...] tagged with the source file so
    we can report per-file stats."""
    out: list[tuple[Path, dict]] = []
    for f in sorted(ROOT.glob(input_glob) if not Path(input_glob).is_absolute() else Path(input_glob).parent.glob(Path(input_glob).name)):
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append((f, json.loads(line)))
            except Exception:
                pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase O — filter upskill dataset by teacher score")
    ap.add_argument("--input", default=DEFAULT_INPUT_GLOB,
                    help="Input JSONL path or glob (default: data/upskill/*.jsonl)")
    ap.add_argument("--min-score", type=int, default=5,
                    help="Drop records with teacher_score < this (default: 5)")
    ap.add_argument("--apply", action="store_true",
                    help="Write filtered output (default: dry-run with stats)")
    ap.add_argument("--output", default="data/upskill/_filtered_dataset.jsonl",
                    help="Output JSONL path (when --apply)")
    args = ap.parse_args(argv)

    pairs = load_records(args.input)
    if not pairs:
        print(f"No records found matching: {args.input}")
        return 1

    by_file: dict[str, list[dict]] = {}
    for f, r in pairs:
        by_file.setdefault(str(f.relative_to(ROOT)), []).append(r)

    print(f"Scanned {len(by_file)} file(s), {len(pairs)} total records, min_score={args.min_score}")
    print()

    kept_records: list[dict] = []
    dropped_count = 0
    for fname, records in sorted(by_file.items()):
        # Filter logic differs by mode:
        #  • corpus mode — score rates the teacher's FIX quality, so
        #    drop low-score records (partial/wrong fixes pollute training)
        #  • gherkin mode — score rates the JUNIOR DRAFT quality, NOT
        #    the rewrite. Low score = bad student input = MORE valuable
        #    training pair (bigger gap to learn from). DON'T filter by
        #    score; only drop empty/errored records.
        #  • Always drop: empty output, student-error skips
        def _keep(r: dict) -> tuple[bool, str]:
            if r.get("skipped_due_to_student_error"):
                return False, "student_error"
            if not (r.get("output") or "").strip():
                return False, "empty_output"
            mode = (r.get("mode") or "").lower()
            is_corpus = mode.startswith("corpus")
            score = r.get("teacher_score", 0)
            if is_corpus and score < args.min_score:
                return False, f"corpus_score<{args.min_score}"
            return True, ""

        kept: list[dict] = []
        for r in records:
            ok, _reason = _keep(r)
            if ok:
                kept.append(r)
        dropped = len(records) - len(kept)
        dropped_count += dropped
        print(f"  {fname}: {len(records)} total → {len(kept)} kept, {dropped} dropped")
        for r in records:
            ok, reason = _keep(r)
            if not ok:
                seed = r.get("seed_id") or r.get("requirement_id") or "?"
                score = r.get("teacher_score", 0)
                print(f"      DROP: {seed} (score {score}, {reason})")
        kept_records.extend(kept)

    print()
    print(f"Total: {len(kept_records)} kept / {dropped_count} dropped of {len(pairs)}")

    if args.apply:
        out_path = ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for r in kept_records:
                f.write(json.dumps(r) + "\n")
        print(f"\nWrote filtered dataset → {out_path.relative_to(ROOT)}")
        print(f"Next: CUDA_VISIBLE_DEVICES=0 python scripts/train_upskill.py "
              f"# (or point DATA_DIR at the filter output)")
    else:
        print("\n(Dry run — pass --apply to write the filtered file.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
