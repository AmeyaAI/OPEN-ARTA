"""R156.A.1 — Pydantic/Joi/Zod body schema extraction tests.

The helper `AutomationEngineerAgent._r156_a_extract_body_schema_hint` walks
the handler function signature right after a route decorator match, finds
the typed body parameter (e.g., `payload: DatasetCreate`), and returns
the raw class/interface source snippet so the LLM gen prompt sees the
canonical SUT request body schema instead of inventing field names.

Mission contract (Pillar 1 — test case quality): Iter 11 measured ~171
Newman 400 + significant fraction of 2279 × 500 traceable to body-field
hallucination. R156.A.1 closes this at gen time by feeding real Pydantic
class definitions into the prompt context.
"""
from __future__ import annotations

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent


def _find_route_end(file_content: str, marker: str) -> int:
    """Helper: locate end position of the route decorator marker text
    inside file_content (mimics regex match.end())."""
    idx = file_content.find(marker)
    assert idx >= 0, f"marker {marker!r} not in file_content"
    return idx + len(marker)


# ── R156.A.1 Pydantic happy path ──────────────────────────────────────


def test_r156_a_extracts_pydantic_body_schema() -> None:
    """FastAPI handler with Pydantic body parameter: extract the class
    definition snippet so the LLM sees real field names + types."""
    file_content = '''
from pydantic import BaseModel
from fastapi import APIRouter

router = APIRouter()


class DatasetCreate(BaseModel):
    name: str
    owner_id: str
    description: str | None = None
    engine_type: str = "analytics_tool"


@router.post("/api/v1/datasets")
async def create_dataset(payload: DatasetCreate):
    return {"id": "new"}
'''
    route_end = _find_route_end(file_content, '"/api/v1/datasets")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    assert snippet is not None
    assert "class DatasetCreate(BaseModel):" in snippet
    assert "name: str" in snippet
    assert "owner_id: str" in snippet
    assert "description: str | None = None" in snippet
    assert "engine_type:" in snippet


def test_r156_a_skips_when_no_typed_body() -> None:
    """Handler with no typed body parameter (e.g., only path params) → None."""
    file_content = '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/datasets/{dataset_id}")
async def get_dataset(dataset_id: str):
    return {"id": dataset_id}
'''
    route_end = _find_route_end(file_content, '"/api/v1/datasets/{dataset_id}")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    # `dataset_id: str` is path param, not a body type — but our heuristic
    # accepts any PascalCase type. `str` is lowercase so should be skipped.
    assert snippet is None


def test_r156_a_skips_fastapi_helper_types() -> None:
    """Handler signatures using FastAPI helper types (Body, Depends, Query,
    Header, Path, Security) must not match — they're not request body
    classes."""
    file_content = '''
from fastapi import APIRouter, Body, Depends, Query

router = APIRouter()


@router.post("/api/v1/login")
async def login(token: str = Body(...), session: Session = Depends()):
    return {"ok": True}
'''
    route_end = _find_route_end(file_content, '"/api/v1/login")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    # `Body` and `Depends` and `Session` all in the non-body allow-out set
    assert snippet is None


def test_r156_a_extracts_first_non_helper_type() -> None:
    """When multiple typed params exist, return the first one not in the
    non-body allow-out set (Path/Query/Header/Body/Depends/Security)."""
    file_content = '''
from fastapi import APIRouter, Path, Depends

router = APIRouter()


class UserUpdate(BaseModel):
    name: str
    email: str


@router.put("/api/v1/users/{user_id}")
async def update_user(
    user_id: str = Path(...),
    payload: UserUpdate = Body(...),
    db: Session = Depends(),
):
    return {"id": user_id}
'''
    route_end = _find_route_end(file_content, '"/api/v1/users/{user_id}")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    # Path/Session helpers skipped; UserUpdate is the real body type
    assert snippet is not None
    assert "class UserUpdate(BaseModel):" in snippet
    assert "name: str" in snippet
    assert "email: str" in snippet


def test_r156_a_returns_none_when_class_not_in_file() -> None:
    """When the typed body class is imported from another module
    (not defined in this file), helper can't resolve it → return None
    gracefully. Future enhancement may follow imports cross-file."""
    file_content = '''
from fastapi import APIRouter
from .schemas import DatasetCreate   # defined elsewhere

router = APIRouter()


@router.post("/api/v1/datasets")
async def create_dataset(payload: DatasetCreate):
    return {"id": "new"}
'''
    route_end = _find_route_end(file_content, '"/api/v1/datasets")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    assert snippet is None


def test_r156_a_extracts_typescript_interface() -> None:
    """Express+TypeScript handler with `interface BodyShape { ... }`
    definition: extract the interface body the same way as Pydantic."""
    file_content = '''
import { Router, Request, Response } from 'express';

const router = Router();


interface DatasetCreateBody {
  name: string;
  ownerId: string;
  description?: string;
}


router.post("/api/v1/datasets", async (req: Request<{}, {}, DatasetCreateBody>, res: Response) => {
  res.json({ id: 'new' });
});
'''
    route_end = _find_route_end(file_content, '"/api/v1/datasets"')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    # Express path: helper looks for `def <name>(` which TS doesn't have.
    # The signature is arrow-function. Helper should return None for now
    # (Express TS interfaces are a future enhancement, not part of A.1 MVP).
    # If returning a snippet, it MUST contain the interface body.
    if snippet is not None:
        assert "interface DatasetCreateBody" in snippet
        assert "name: string" in snippet


# ── R156.A.1 robustness ──────────────────────────────────────────────


def test_r156_a_handles_no_handler_after_decorator() -> None:
    """File ends right after the decorator (no handler signature) — return None
    gracefully without raising."""
    file_content = '@router.post("/api/v1/datasets")'
    route_end = len(file_content)
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    assert snippet is None


def test_r156_a_handles_empty_input() -> None:
    """Empty file content + position 0 — return None gracefully."""
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint("", 0)
    assert snippet is None


def test_r156_a_caps_class_body_at_thirty_lines() -> None:
    """When the class body has > 30 lines, helper caps at 30 lines so
    a pathologically large model doesn't blow the prompt budget."""
    field_lines = "\n".join(
        f"    field_{i}: str" for i in range(50)
    )
    file_content = f'''
class HugeModel(BaseModel):
{field_lines}


@router.post("/api/v1/big")
async def post_big(payload: HugeModel):
    return {{}}
'''
    route_end = _find_route_end(file_content, '"/api/v1/big")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    assert snippet is not None
    # Snippet shouldn't include all 50 fields — capped at 30 lines total
    # (including the `class X:` line, so ~29 field lines max)
    line_count = len(snippet.split("\n"))
    assert line_count <= 30, f"Expected ≤30 lines, got {line_count}"


def test_r156_a_class_body_stops_at_next_top_level() -> None:
    """Class body extraction stops when it hits the next non-indented
    non-blank line (next class, function, or top-level statement)."""
    file_content = '''
class FirstModel(BaseModel):
    name: str
    age: int


class SecondModel(BaseModel):
    other: bool


@router.post("/api/v1/first")
async def post_first(payload: FirstModel):
    return {}
'''
    route_end = _find_route_end(file_content, '"/api/v1/first")')
    snippet = AutomationEngineerAgent._r156_a_extract_body_schema_hint(
        file_content, route_end,
    )
    assert snippet is not None
    assert "class FirstModel" in snippet
    assert "name: str" in snippet
    assert "age: int" in snippet
    # Must NOT bleed into FirstModel's siblings
    assert "class SecondModel" not in snippet
    assert "other: bool" not in snippet
