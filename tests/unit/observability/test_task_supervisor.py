"""F9-11: Cover the Gap-1.5 task_supervisor — done-callback for unhandled
exceptions on background asyncio tasks. Without this, every supervise()
site silently swallows exceptions if the underlying logging or callback
behaviour regresses.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from src.observability.task_supervisor import supervise


class TestSupervise:

    async def test_logs_exception_when_task_raises(self, caplog):
        async def boom():
            raise RuntimeError("forced failure")
        with caplog.at_level(logging.ERROR, logger="arta.supervisor"):
            t = asyncio.create_task(boom())
            supervise(t, "boom-task")
            with pytest.raises(RuntimeError):
                await t
            # Allow the done-callback to run
            await asyncio.sleep(0)
        assert any("boom-task" in r.message for r in caplog.records)

    async def test_invokes_on_error_callback_with_exception(self):
        captured = []
        def on_err(exc):
            captured.append(exc)
        async def boom():
            raise ValueError("captured")
        t = asyncio.create_task(boom())
        supervise(t, "callback-task", on_error=on_err)
        with pytest.raises(ValueError):
            await t
        await asyncio.sleep(0)
        assert len(captured) == 1
        assert isinstance(captured[0], ValueError)

    async def test_silent_when_task_succeeds(self, caplog):
        async def ok():
            return 42
        with caplog.at_level(logging.ERROR, logger="arta.supervisor"):
            t = asyncio.create_task(ok())
            supervise(t, "ok-task")
            assert await t == 42
            await asyncio.sleep(0)
        # No error logs for a clean task
        assert not any("ok-task" in r.message and r.levelno >= logging.ERROR for r in caplog.records)

    async def test_swallows_exception_inside_on_error_callback(self):
        # If the operator's on_error itself raises, supervise must not crash
        # the event loop. It logs and moves on.
        async def boom():
            raise RuntimeError("primary")
        def bad_callback(_exc):
            raise KeyError("secondary")
        t = asyncio.create_task(boom())
        supervise(t, "double-fault", on_error=bad_callback)
        with pytest.raises(RuntimeError):
            await t
        await asyncio.sleep(0)
        # If we reach here without an unhandled exception escaping, supervise survived
