"""Gap-1.5: Supervise asyncio.create_task() invocations so unhandled exceptions
are logged rather than silently swallowed.

Use `supervise(task, name)` immediately after `asyncio.create_task(...)`:

    task = asyncio.create_task(do_work())
    supervise(task, "do_work")

If `do_work()` raises, the exception is logged with the task name — not lost.
Optionally pass `on_error` to invoke a custom callback (e.g. mark a DB record).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger("arta.supervisor")


def supervise(task: asyncio.Task, name: str, on_error: Callable | None = None) -> asyncio.Task:
    """Attach a done-callback that logs unhandled exceptions.

    Returns the same task (for chaining).
    """
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            log.debug("Task %s cancelled", name)
            return
        exc = t.exception()
        if exc is None:
            return
        log.exception("Task %s crashed: %s", name, exc, exc_info=exc)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception as cb_exc:
                log.warning("Task %s on_error callback raised: %s", name, cb_exc)

    task.add_done_callback(_on_done)
    return task
