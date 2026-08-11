"""F9-7: Cover the F8-2 teacher factory + adapters.

We don't actually call litellm — only verify the routing logic.
"""
from __future__ import annotations

import os

import pytest

from scripts.upskill.teachers import (
    ClaudeTeacher,
    GeminiTeacher,
    available_teachers,
    make_teacher,
)


class TestMakeTeacher:

    def test_default_is_gemini(self):
        os.environ.pop("ARTA_UPSKILL_TEACHER", None)
        os.environ.pop("ARTA_UPSKILL_TEACHER_MODEL", None)
        t = make_teacher()
        assert isinstance(t, GeminiTeacher)
        assert t.name == "gemini"
        assert t.model == GeminiTeacher.default_model

    def test_explicit_claude_returns_claude_adapter(self):
        os.environ.pop("ARTA_UPSKILL_TEACHER_MODEL", None)
        t = make_teacher("claude")
        assert isinstance(t, ClaudeTeacher)
        assert t.name == "claude"
        assert t.model == ClaudeTeacher.default_model

    def test_env_var_selects_teacher(self):
        os.environ["ARTA_UPSKILL_TEACHER"] = "claude"
        os.environ.pop("ARTA_UPSKILL_TEACHER_MODEL", None)
        try:
            t = make_teacher()
            assert isinstance(t, ClaudeTeacher)
        finally:
            del os.environ["ARTA_UPSKILL_TEACHER"]

    def test_unknown_teacher_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown teacher"):
            make_teacher("mistral")

    def test_explicit_model_arg_overrides_default(self):
        t = make_teacher("claude", model="claude-haiku-4-5-20251001")
        assert t.model == "claude-haiku-4-5-20251001"

    def test_env_model_override_applies(self):
        os.environ["ARTA_UPSKILL_TEACHER_MODEL"] = "custom-id"
        try:
            t = make_teacher("gemini")
            assert t.model == "custom-id"
        finally:
            del os.environ["ARTA_UPSKILL_TEACHER_MODEL"]

    def test_available_teachers_lists_both(self):
        # Sanity: future Mistral additions should bump this list
        names = available_teachers()
        assert "gemini" in names and "claude" in names
