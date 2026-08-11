"""F7-2 (M2 partial) — test-data fixtures sub-router.

Carved out of `tests.py` so the fixtures feature lives on its own. Endpoints
mount under `/api/tests/{test_id}/data` via `router.include_router(...)`.

Endpoints:
  GET  /{test_id}/data                       — read inline fixture rows
  POST /{test_id}/data                       — upsert inline fixture rows
  GET  /{test_id}/fixture-data               — file-based frozen-dataset preview
  POST /{test_id}/fixture-data/materialise   — generate the frozen dataset

Mock fixtures (when DB unavailable) live in MOCK_FIXTURES below; real reads
go through the existing TestDataFixtureRepo.
"""
from __future__ import annotations

import csv as _csv
import json as _json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..dependencies import require_api_key as _require_api_key

from ...agents.sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

log = logging.getLogger("arta.fixtures")


# Sub-router; mounted by tests.py's router.include_router(fixtures_router)
fixtures_router = APIRouter()


# ── Mock fixture seed (dev / DB-unavailable fallback) ────────────────────────
MOCK_FIXTURES: dict[str, dict] = {
    "TC-124": {
        "fixture_id": "TD-124", "test_id": "TC-124",
        "description": "card boundary test data",
        "columns": ["qty", "card_type", "card_number", "expiry", "cvv", "timeout_s", "expected_status"],
        "rows": [
            {"qty": 1, "card_type": "Visa", "card_number": "4111111111111111", "expiry": "12/28", "cvv": "123", "timeout_s": 3, "expected_status": 202},
            {"qty": 2, "card_type": "Mastercard", "card_number": "5500005555555559", "expiry": "09/27", "cvv": "456", "timeout_s": 3, "expected_status": 202},
            {"qty": 1, "card_type": "Amex", "card_number": "371449635398431", "expiry": "03/26", "cvv": "7890", "timeout_s": 5, "expected_status": 202},
            {"qty": 0, "card_type": "Visa", "card_number": "4111111111111111", "expiry": "12/28", "cvv": "123", "timeout_s": 1, "expected_status": 400},
        ],
    },
    "TC-126": {
        "fixture_id": "TD-126", "test_id": "TC-126",
        "description": "load profile scenarios",
        "columns": ["vus", "ramp_up_s", "plateau_s", "threshold_p95_ms", "max_err_pct", "tier"],
        "rows": [
            {"vus": 100, "ramp_up_s": 30, "plateau_s": 60,  "threshold_p95_ms": 1000, "max_err_pct": 0.5, "tier": "tier1"},
            {"vus": 200, "ramp_up_s": 30, "plateau_s": 120, "threshold_p95_ms": 2000, "max_err_pct": 0.5, "tier": "tier2"},
            {"vus": 500, "ramp_up_s": 60, "plateau_s": 120, "threshold_p95_ms": 3000, "max_err_pct": 1.0, "tier": "tier2"},
        ],
    },
    "TC-127": {
        "fixture_id": "TD-127", "test_id": "TC-127",
        "description": "expired card boundary values",
        "columns": ["card_number", "expiry", "expected_status", "expected_error_code"],
        "rows": [
            {"card_number": "4111111111111111", "expiry": "01/20", "expected_status": 422, "expected_error_code": "card_expired"},
            {"card_number": "4111111111111111", "expiry": "12/99", "expected_status": 422, "expected_error_code": "card_expired"},
            {"card_number": "4111111111111111", "expiry": "01/28", "expected_status": 202, "expected_error_code": None},
        ],
    },
}


class FixtureUpsertRequest(BaseModel):
    description: str | None = None
    columns: list[str] = []
    rows: list[dict] = []
    examples: str | None = None


@fixtures_router.get("/{test_id}/data", dependencies=[Depends(_require_api_key)])
async def get_test_data(test_id: str):
    """Return fixture rows for a test case."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import TestDataFixtureRepo, _to_dict
            repo = TestDataFixtureRepo(db)
            rows = await repo.list_for_test(test_id.upper())
            if rows:
                return _to_dict(rows[0])

    fixture = MOCK_FIXTURES.get(test_id.upper())
    if not fixture:
        return {"test_id": test_id, "fixture_id": None, "columns": [], "rows": [],
                "message": "No fixtures yet — run /generate-tests to create"}
    return fixture


@fixtures_router.post("/{test_id}/data", dependencies=[Depends(_require_api_key)])
async def upsert_test_data(test_id: str, body: FixtureUpsertRequest):
    """Upsert fixture rows for a test case."""
    from ..db_adapter import try_db

    tid = test_id.upper()

    async with try_db() as db:
        if db:
            from ...db.repository import TestDataFixtureRepo
            repo = TestDataFixtureRepo(db)
            await repo.create({
                "test_id": tid,
                "description": body.description or "",
                "columns": body.columns,
                "rows": body.rows,
            })
            return {"ok": True, "test_id": tid, "row_count": len(body.rows)}

    existing = MOCK_FIXTURES.get(tid, {"fixture_id": f"TD-{tid}", "test_id": tid})
    MOCK_FIXTURES[tid] = {
        **existing,
        "columns": body.columns,
        "rows": body.rows,
        "description": body.description or existing.get("description", ""),
        "examples": body.examples,
    }
    return {"ok": True, "test_id": tid, "row_count": len(body.rows)}


# ── Frozen-dataset preview ───────────────────────────────────────────────────
# Surfaces the actual data (or its absence) referenced by Gherkin steps like
#   Given frozen_dataset("fixtures/req_am_020_fixture.parquet")
# so testers can review what the test will run against, not just the path.

_FIXTURE_PATH_RE = re.compile(
    r"""(?ix)
    (?:fixtures?/[^\s"')\]]+\.(?:parquet|csv|json|jsonl|tsv|zip|pdf|xlsx|docx))
    """
)
_SAMPLE_ROW_LIMIT = 20
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _extract_fixture_paths(gherkin: str) -> list[str]:
    """Pull every unique `fixtures/...ext` reference out of the Gherkin text.

    Returns paths in the order they appear (de-duplicated). Matches both the
    canonical `frozen_dataset("fixtures/...")` form and any bare reference.
    """
    if not gherkin:
        return []
    seen: list[str] = []
    for m in _FIXTURE_PATH_RE.finditer(gherkin):
        path = m.group(0).strip()
        if path not in seen:
            seen.append(path)
    return seen


def _summarise_columns(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    """Per-column dtype/null/distinct stats over the loaded sample. Cheap and
    deterministic — no extra deps. Numeric columns also surface min/max."""
    summary: list[dict[str, Any]] = []
    for col in columns:
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v not in (None, "", "null")]
        numeric: list[float] = []
        for v in non_null:
            try:
                numeric.append(float(v))
            except (TypeError, ValueError):
                numeric = []
                break
        dtype = "number" if numeric else "string"
        entry: dict[str, Any] = {
            "name": col,
            "dtype": dtype,
            "null_pct": round((len(values) - len(non_null)) / max(len(values), 1) * 100, 1),
            "distinct": len({str(v) for v in non_null}),
        }
        if numeric:
            entry["min"] = min(numeric)
            entry["max"] = max(numeric)
        summary.append(entry)
    return summary


def _read_csv_preview(path: Path) -> dict[str, Any]:
    """Read columns, total row count, and a 20-row sample from a CSV/TSV."""
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    columns: list[str] = []
    sample: list[dict[str, Any]] = []
    total = 0
    with path.open("r", newline="", errors="replace") as f:
        reader = _csv.DictReader(f, delimiter=delim)
        columns = list(reader.fieldnames or [])
        for row in reader:
            total += 1
            if len(sample) < _SAMPLE_ROW_LIMIT:
                sample.append(row)
    return {
        "format": path.suffix.lstrip(".").lower(),
        "row_count": total,
        "columns": columns,
        "sample_rows": sample,
        "column_summary": _summarise_columns(sample, columns),
    }


def _read_json_preview(path: Path) -> dict[str, Any]:
    """Read JSON or JSONL into a sample. Arrays of dicts are expected; other
    shapes (single object, scalar) get wrapped/labelled gracefully."""
    text = path.read_text(errors="replace")
    rows: list[dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except _json.JSONDecodeError:
                continue
    else:
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError as exc:
            return {"format": "json", "error": f"Invalid JSON: {exc}"}
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            rows = [data]
    columns: list[str] = []
    for r in rows[: _SAMPLE_ROW_LIMIT * 2]:
        for k in r.keys():
            if k not in columns:
                columns.append(k)
    sample = rows[:_SAMPLE_ROW_LIMIT]
    return {
        "format": path.suffix.lstrip(".").lower(),
        "row_count": len(rows),
        "columns": columns,
        "sample_rows": sample,
        "column_summary": _summarise_columns(sample, columns),
    }


def _read_parquet_preview(path: Path) -> dict[str, Any]:
    """Best-effort parquet preview. Returns a stub when pyarrow is unavailable
    so the caller still gets file metadata + a clear note that the preview
    needs `pip install pyarrow`."""
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return {
            "format": "parquet",
            "preview_unavailable": True,
            "message": "Parquet preview requires pyarrow — install it to enable, or click Materialise to regenerate as CSV.",
        }
    try:
        table = pq.read_table(path)
    except Exception as exc:
        return {"format": "parquet", "error": f"Could not read parquet: {exc}"}
    columns = list(table.column_names)
    total = table.num_rows
    sample_table = table.slice(0, _SAMPLE_ROW_LIMIT)
    sample_rows = sample_table.to_pylist()
    return {
        "format": "parquet",
        "row_count": total,
        "columns": columns,
        "sample_rows": sample_rows,
        "column_summary": _summarise_columns(sample_rows, columns),
    }


def _resolve_fixture_path(rel: str) -> Path:
    """Resolve a Gherkin-mentioned path against the repo root (joined paths
    that try to escape are still bounded — `_REPO_ROOT.resolve()` strips
    `..` segments before the existence check below)."""
    return (_REPO_ROOT / rel).resolve()


def _preview_for_path(rel: str) -> dict[str, Any]:
    """Build a single fixture entry for the response. Includes existence flag,
    file metadata (size, mtime), and a format-specific preview."""
    abs_path = _resolve_fixture_path(rel)
    entry: dict[str, Any] = {
        "path": rel,
        "absolute_path": str(abs_path),
        "exists": abs_path.exists() and abs_path.is_file(),
        "format": abs_path.suffix.lstrip(".").lower(),
    }
    if not entry["exists"]:
        # Surface neighbours materialised by generator.py (which writes
        # `{req_slug}_dataset_v1_0_0.{ext}` rather than the LLM-imagined name).
        analytics_dir = _REPO_ROOT / "fixtures" / "analytics"
        if analytics_dir.exists():
            siblings = sorted(analytics_dir.glob("*"))
            if siblings:
                entry["alternatives"] = [str(s.relative_to(_REPO_ROOT)) for s in siblings[:10]]
        return entry
    stat = abs_path.stat()
    entry["size_bytes"] = stat.st_size
    entry["modified_at"] = stat.st_mtime
    suffix = abs_path.suffix.lower()
    try:
        if suffix in (".csv", ".tsv"):
            entry.update(_read_csv_preview(abs_path))
        elif suffix in (".json", ".jsonl"):
            entry.update(_read_json_preview(abs_path))
        elif suffix == ".parquet":
            entry.update(_read_parquet_preview(abs_path))
        else:
            entry["preview_unavailable"] = True
            entry["message"] = f"No preview support for .{suffix.lstrip('.')} (binary asset)."
    except Exception as exc:
        log.warning("fixture preview failed for %s: %s", abs_path, exc)
        entry["error"] = str(exc)
    return entry


@fixtures_router.get("/{test_id}/fixture-data", dependencies=[Depends(_require_api_key)])
async def get_fixture_data(test_id: str) -> dict[str, Any]:
    """Parse the test's Gherkin for `fixtures/...` references and return a
    preview (columns + sample rows + column summary) for each one. Files
    that don't exist on disk come back with `exists: false` so the UI can
    offer a "Materialise" button.
    """
    from ..db_adapter import try_db

    tid = test_id.upper()
    gherkin = ""
    requirement_id_text = ""

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseRepo
            tc = await TestCaseRepo(db).get(tid)
            if tc is None:
                raise HTTPException(status_code=404, detail=f"Test {tid} not found")
            gherkin = tc.gherkin_scenario or ""
            if tc.requirement_id is not None:
                from sqlalchemy import select as _sel
                from ...db.models import Requirement as _Req
                row = (await db.execute(_sel(_Req).where(_Req.id == tc.requirement_id))).scalars().first()
                if row is not None:
                    requirement_id_text = row.req_id

    refs = _extract_fixture_paths(gherkin)
    primary = [_preview_for_path(r) for r in refs]
    for entry in primary:
        entry["purpose"] = "primary"

    # When the Gherkin's literal path is missing but a sibling fixture exists
    # (the LLM-imagined `req_am_020_fixture.parquet` vs the canonical
    # `req_am_020_dataset_v1_0_0.csv` materialise_fixture writes), surface
    # the sibling's data inline so testers can review it without clicking
    # Materialise. De-duplicate so the same alt isn't shown twice.
    primary_paths = {e["path"] for e in primary}
    alt_paths_seen: set[str] = set()
    alt_entries: list[dict[str, Any]] = []
    for entry in primary:
        if entry.get("exists"):
            continue
        for alt in entry.get("alternatives", []) or []:
            if alt in primary_paths or alt in alt_paths_seen:
                continue
            alt_paths_seen.add(alt)
            alt_preview = _preview_for_path(alt)
            alt_preview["purpose"] = "alternative"
            alt_preview["alternative_for"] = entry["path"]
            alt_entries.append(alt_preview)

    return {
        "test_id": tid,
        "requirement_id": requirement_id_text,
        "datasets": primary + alt_entries,
        "sample_row_limit": _SAMPLE_ROW_LIMIT,
    }


class MaterialiseRequest(BaseModel):
    columns: list[str] | None = None
    row_count: int = 1000
    version: str = "1.0.0"
    fmt: str = "parquet"


@fixtures_router.post("/{test_id}/fixture-data/materialise",
                      dependencies=[Depends(_require_api_key)])
async def materialise_fixture_for_test(
    test_id: str,
    body: MaterialiseRequest | None = None,
) -> dict[str, Any]:
    """Generate the frozen dataset for this test's requirement. Backed by
    src.fixtures.generator.materialise_fixture — deterministic on
    (req_id, version) so re-running yields the same file.
    """
    from ..db_adapter import try_db

    tid = test_id.upper()
    body = body or MaterialiseRequest()

    req_text = ""
    async with try_db() as db:
        if not db:
            raise HTTPException(status_code=503, detail="Database unavailable")
        from ...db.repository import TestCaseRepo
        from sqlalchemy import select as _sel
        from ...db.models import Requirement as _Req
        tc = await TestCaseRepo(db).get(tid)
        if tc is None:
            raise HTTPException(status_code=404, detail=f"Test {tid} not found")
        if tc.requirement_id is None:
            raise HTTPException(
                status_code=400,
                detail="Test case has no linked requirement — cannot materialise fixture",
            )
        row = (await db.execute(_sel(_Req).where(_Req.id == tc.requirement_id))).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Linked requirement not found")
        req_text = row.req_id

    # Phase 2.3 — load the persisted recipe sidecar (written at
    # .arta/recipes/{req_slug}_v{ver}.json by Stage 1.5) so the UI's
    # "Materialise fixture" button uses the recipe-driven primitives instead
    # of the column-name heuristic. Falls back gracefully when no recipe
    # exists yet (legacy reqs / non-analytics projects).
    recipe_dict: dict | None = None
    try:
        req_slug = sanitize_req_id(req_text)
        ver_slug = body.version.replace(".", "_")
        sidecar = _REPO_ROOT / ".arta" / "recipes" / f"{req_slug}_v{ver_slug}.json"
        if sidecar.is_file():
            recipe_dict = _json.loads(sidecar.read_text())
            log.info("materialise: loaded recipe sidecar %s", sidecar)
    except Exception as exc:
        log.debug("materialise: recipe sidecar load skipped: %s", exc)

    try:
        from ...fixtures.generator import materialise_fixture
        out_path = materialise_fixture(
            req_id=req_text,
            columns=body.columns,
            row_count=body.row_count,
            version=body.version,
            fmt=body.fmt,
            recipe=recipe_dict,
        )
    except Exception as exc:
        log.warning("materialise failed for %s: %s", req_text, exc)
        raise HTTPException(status_code=500, detail=f"Materialise failed: {exc}")

    rel = str(Path(out_path).resolve().relative_to(_REPO_ROOT))
    return {
        "ok": True,
        "test_id": tid,
        "requirement_id": req_text,
        "path": rel,
        "preview": _preview_for_path(rel),
    }


# ── Helpers shared by download + review ──────────────────────────────────────


async def _gherkin_for_test(test_id: str) -> tuple[str, str]:
    """Load `(gherkin_scenario, requirement_id_text)` for a test_id, or 404."""
    from ..db_adapter import try_db
    async with try_db() as db:
        if not db:
            raise HTTPException(status_code=503, detail="Database unavailable")
        from ...db.repository import TestCaseRepo
        tc = await TestCaseRepo(db).get(test_id.upper())
        if tc is None:
            raise HTTPException(status_code=404, detail=f"Test {test_id} not found")
        gherkin = tc.gherkin_scenario or ""
        req_text = ""
        if tc.requirement_id is not None:
            from sqlalchemy import select as _sel
            from ...db.models import Requirement as _Req
            row = (await db.execute(_sel(_Req).where(_Req.id == tc.requirement_id))).scalars().first()
            if row is not None:
                req_text = row.req_id
        return gherkin, req_text


def _allowed_paths(gherkin: str) -> set[str]:
    """The full allow-list for a test: refs from Gherkin + their alternatives.
    Used to gate the download endpoint so a user can't grab arbitrary disk files."""
    refs = _extract_fixture_paths(gherkin)
    allowed: set[str] = set(refs)
    analytics_dir = _REPO_ROOT / "fixtures" / "analytics"
    if analytics_dir.exists():
        for p in analytics_dir.glob("*"):
            try:
                allowed.add(str(p.relative_to(_REPO_ROOT)))
            except ValueError:
                continue
    return allowed


# ── Download ────────────────────────────────────────────────────────────────


@fixtures_router.get("/{test_id}/fixture-data/download",
                     dependencies=[Depends(_require_api_key)])
async def download_fixture(test_id: str, path: str) -> FileResponse:
    """Stream the requested fixture file. The path must already be referenced
    (directly or via a sibling alternative) by this test's Gherkin — arbitrary
    disk reads are rejected with 403.
    """
    gherkin, _ = await _gherkin_for_test(test_id)
    allowed = _allowed_paths(gherkin)
    if path not in allowed:
        raise HTTPException(status_code=403, detail="Fixture not associated with this test")

    abs_path = _resolve_fixture_path(path)
    # Defense in depth: even with the allow-list check, ensure the resolved
    # absolute path stays under the repo root after symlink/`..` resolution.
    try:
        abs_path.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside fixtures root")
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not on disk: {path}")
    return FileResponse(
        abs_path,
        media_type="application/octet-stream",
        filename=abs_path.name,
    )


# ── Review (LLM-backed Gherkin ↔ data alignment check) ───────────────────────


_REVIEW_PROMPT = """\
You are an ATDD reviewer. Compare the BDD scenario to the actual test data \
that will feed it, and flag concrete mismatches.

GHERKIN SCENARIO:
{gherkin}

DATASET (one of the frozen fixtures referenced by the scenario):
- path: {path}
- format: {fmt}
- row_count: {rows}
- columns: {cols}
- column_summary: {summary}
- sample_rows (first {n}): {sample}

For each assertion in `Then`/`And` steps, decide whether the dataset can \
plausibly satisfy it. Watch for:
  - Assertions on properties/metrics not present (or unrelated to) the data \
    columns — e.g. `insight.metric == "sales"` when no `metric` column exists.
  - Magnitude/direction expectations that random data won't produce \
    deterministically (e.g. "should trend up by 12.5%").
  - Snapshot/version pins in Background that the file path doesn't reflect.

Output ONLY JSON in this shape, no prose:
{{
  "verdict": "match" | "partial" | "mismatch",
  "summary": "<one or two sentences>",
  "findings": [
    {{
      "level": "info" | "warn" | "error",
      "step": "<the Gherkin line if relevant, else empty>",
      "message": "<short explanation>"
    }}
  ]
}}
"""


def _structural_review(gherkin: str, ds: dict[str, Any]) -> dict[str, Any]:
    """Cheap, deterministic fallback when the LLM client is unavailable.

    Walks Then/And steps, extracts dotted property names, and warns when those
    names don't appear as fixture columns. Catches the common case where a
    scenario asserts on `insight.metric` etc. while the fixture only has
    raw inputs."""
    cols = {c.lower() for c in (ds.get("columns") or [])}
    # File-extension suffixes that look like `name.parquet` but aren't dotted
    # property accesses — skip these so we don't flag `data_snapshot = "...parquet"`
    # as an assertion on a `parquet` column.
    file_exts = {"parquet", "csv", "tsv", "json", "jsonl", "zip", "pdf", "xlsx", "docx"}
    skip_objs = {"row", "data_snapshot", "model_version", "prompt_version", "fixtures"}
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for line in (gherkin or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("Then ") or stripped.startswith("And ")):
            continue
        for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", stripped, re.IGNORECASE):
            prop = m.group(2).lower()
            obj = m.group(1).lower()
            if obj in skip_objs or prop in file_exts:
                continue
            # The dotted match must be inside an assertion idiom — skip when the
            # surrounding line is an assignment like `And foo.bar = "..."`,
            # which is a Background snapshot pin, not a Then-style assertion.
            if re.search(rf"\b{re.escape(m.group(0))}\s*=", stripped):
                continue
            key = (obj, prop)
            if key in seen:
                continue
            seen.add(key)
            if cols and prop not in cols:
                findings.append({
                    "level": "warn",
                    "step": stripped,
                    "message": (
                        f"Step asserts on `{m.group(0)}` but the fixture has no `{prop}` column. "
                        "This may be an output-side property; check the pipeline produces it."
                    ),
                })
    if not findings:
        return {
            "verdict": "match",
            "summary": "Structural check passed: every dotted property in Then/And steps maps to a fixture column. (Heuristic only — no LLM available.)",
            "findings": [],
        }
    verdict = "mismatch" if any(f["level"] == "error" for f in findings) else "partial"
    return {
        "verdict": verdict,
        "summary": f"Structural check found {len(findings)} potential mismatch(es). LLM review unavailable.",
        "findings": findings,
    }


@fixtures_router.post("/{test_id}/fixture-data/review",
                      dependencies=[Depends(_require_api_key)])
async def review_fixture_data(test_id: str, request: Request) -> dict[str, Any]:
    """Compare the test's Gherkin to the actual data and surface mismatches.

    Picks the first dataset that exists on disk (primary ref or sibling
    alternative) and asks Claude to map every Then/And assertion against the
    fixture's columns + sample. Falls back to a structural-only check when no
    LLM client is configured."""
    gherkin, _req = await _gherkin_for_test(test_id)
    refs = _extract_fixture_paths(gherkin)
    if not refs:
        return {
            "verdict": "info",
            "summary": "No frozen-dataset references in the Gherkin — nothing to review.",
            "findings": [],
        }

    chosen: dict[str, Any] | None = None
    for r in refs:
        preview = _preview_for_path(r)
        if preview.get("exists"):
            chosen = preview
            break
    if chosen is None:
        analytics_dir = _REPO_ROOT / "fixtures" / "analytics"
        if analytics_dir.exists():
            for sib in sorted(analytics_dir.glob("*")):
                rel = str(sib.relative_to(_REPO_ROOT))
                preview = _preview_for_path(rel)
                if preview.get("exists"):
                    chosen = preview
                    break
    if chosen is None:
        return {
            "verdict": "info",
            "summary": "Gherkin references a fixture but no file is on disk yet — click Materialise first.",
            "findings": [],
        }

    client = getattr(request.app.state, "anthropic", None)
    if client is None:
        review = _structural_review(gherkin, chosen)
        review["dataset_path"] = chosen.get("path")
        return review

    prompt = _REVIEW_PROMPT.format(
        gherkin=gherkin[:6000],
        path=chosen.get("path"),
        fmt=chosen.get("format"),
        rows=chosen.get("row_count"),
        cols=chosen.get("columns"),
        summary=_json.dumps(chosen.get("column_summary", []))[:2000],
        sample=_json.dumps(chosen.get("sample_rows", [])[:5], default=str)[:2500],
        n=min(5, len(chosen.get("sample_rows", []) or [])),
    )
    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Tolerate ```json fences and reasoning blocks.
        text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
        review = _json.loads(text)
    except Exception as exc:
        log.warning("LLM review failed for %s: %s", test_id, exc)
        review = _structural_review(gherkin, chosen)
    review["dataset_path"] = chosen.get("path")
    return review
