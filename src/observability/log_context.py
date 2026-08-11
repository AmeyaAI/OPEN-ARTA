"""F1-5: trace_id injection into Python logging via contextvars.

Every generation request sets a trace_id UUID. The agents downstream (strategy,
ATDD, automation, etc.) run as coroutines — contextvars propagate across
asyncio.create_task boundaries automatically, so the trace_id follows the whole
stage chain without each agent needing to thread it manually.

Output format (configured in main.py):
  09:12:34 INFO  [arta.tests] [trace=c26a52f6] [REQ-XY-001] Stage 1/4: Risk scoring

Usage:
  from src.observability.log_context import trace_id_var, set_trace_id

  # At the top of a request:
  set_trace_id(my_uuid)

  # All log lines within the same asyncio context/task chain carry the field.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

# Module-level context variable.
# Default "" shows up as an empty string in formatters when no trace is active
# (normal HTTP handlers, startup, etc.).
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("arta_trace_id", default="")


def set_trace_id(trace_id: str | None) -> contextvars.Token:
    """Set the active trace_id for the current asyncio context.

    Returns a Token — call trace_id_var.reset(token) to restore the prior value
    if you want strict scoping. Most call sites don't need to reset because a
    new request creates a new context.
    """
    return trace_id_var.set(trace_id or "")


def get_trace_id() -> str:
    """Return the active trace_id, or empty string if unset."""
    return trace_id_var.get()


class TraceIdFilter(logging.Filter):
    """Logging filter that attaches the active trace_id to every LogRecord.

    Installed once at application startup. Works with the stdlib formatter's
    `%(trace_id)s` / `%(trace_short)s` placeholders without touching individual
    log.info() callers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tid = trace_id_var.get()
        # Full UUID + short 8-char form for readability in terminal logs
        record.trace_id = tid or "-"
        record.trace_short = (tid[:8] if tid else "-")
        return True


def install(root_logger: logging.Logger | None = None) -> None:
    """Attach the TraceIdFilter to the root logger + all handlers currently installed.

    Call this once from main.py after logging.basicConfig(). Idempotent —
    re-registering the same filter is cheap.
    """
    _filter = TraceIdFilter()
    root = root_logger or logging.getLogger()
    # Filter on root catches loggers that propagate (all arta.* loggers do).
    if not any(isinstance(f, TraceIdFilter) for f in root.filters):
        root.addFilter(_filter)
    for handler in root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(_filter)
