"""FE-sync P4 — preventive contract-drift guard.

The ARTA frontend/backend contract is hand-maintained (hand-written TS types calling
string `/api/...` paths). When a backend route is renamed/removed, the FE call silently
404s at runtime. This guard fails at TEST time instead: it diffs every `/api/...` path the
frontend calls against the backend's OWN OpenAPI schema (a STATIC `app.openapi()` dump —
no running server), matching at the path-template level (param NAMES erased, so the FE's
`${runId}` and the backend's `{run_id}` compare equal).

Path-level only (not full request/response shape) — cheap, catches the dead-call/rename
class. Response-shape drift stays covered by the `missionReport.ts` helper discipline +
defensive rendering.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_FE_SRC = _REPO / "frontend" / "src"

# `/api/...` inside a backtick/quote, up to the first quote / query / whitespace.
_API_LITERAL = re.compile(r"""[`'"](/api/[^`'"?\s]*)""")


def _erase_params(path: str) -> str:
    """Collapse every `{...}` path param to `{}` so param NAMES don't matter."""
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def _strip_comments(text: str) -> str:
    """Drop // line + /* */ block comments so a commented `/api/...` (e.g. the SUT's
    own `/api/v1/users/me` probe, or a doc-block) isn't mistaken for a real FE call."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _normalize_fe_path(raw: str) -> str:
    """A `${...}` is a PATH PARAM only when preceded by '/'. A `${...}` NOT preceded by
    '/' is a query/suffix concat (e.g. `/api/x${query}` where query='?a=b'|'') — truncate
    the path there. Then `/${param}` -> `/{}` and erase param names."""
    raw = re.sub(r"(?<!/)\$\{[^}]*\}.*$", "", raw)   # drop query/suffix concats + trailing
    raw = re.sub(r"\$\{[^}]*\}", "{}", raw)          # remaining are real path params
    return _erase_params(raw)


def _fe_called_api_paths() -> dict[str, str]:
    """{normalized_path: first_source_file} for every `/api/...` literal the FE calls."""
    out: dict[str, str] = {}
    for f in _FE_SRC.rglob("*.ts*"):
        text = _strip_comments(f.read_text(errors="ignore"))
        for m in _API_LITERAL.finditer(text):
            norm = _normalize_fe_path(m.group(1))
            # skip bare `/api` fragments from string concatenation (< 1 real segment)
            if norm.count("/") < 2:
                continue
            out.setdefault(norm, str(f.relative_to(_REPO)))
    return out


def _backend_openapi_paths() -> set[str]:
    from src.api.main import app  # module-level app; .openapi() needs no running server
    return {_erase_params(p) for p in app.openapi().get("paths", {})}


@pytest.mark.skipif(not _FE_SRC.is_dir(), reason="frontend/src not present")
def test_frontend_api_calls_exist_in_backend_openapi():
    backend = _backend_openapi_paths()
    fe = _fe_called_api_paths()
    missing = {p: src for p, src in fe.items() if p not in backend}
    assert not missing, (
        "Frontend calls /api paths that the backend OpenAPI does NOT define "
        "(dead call / renamed-or-removed route):\n"
        + "\n".join(f"  {p}\n      first seen: {src}" for p, src in sorted(missing.items()))
    )
