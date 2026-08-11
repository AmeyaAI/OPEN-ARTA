"""F9-11: Cover the F1-5 trace_id contextvar + logging filter that F8-4 builds on."""
from __future__ import annotations

import asyncio
import logging

from src.observability.log_context import (
    TraceIdFilter,
    get_trace_id,
    set_trace_id,
    trace_id_var,
)


class TestLogContext:

    def test_set_and_get_round_trip(self):
        token = set_trace_id("abcdef-12345")
        try:
            assert get_trace_id() == "abcdef-12345"
        finally:
            trace_id_var.reset(token)

    def test_filter_attaches_trace_id_to_record(self):
        token = set_trace_id("xyz-99")
        try:
            filt = TraceIdFilter()
            record = logging.LogRecord(
                name="t", level=logging.INFO, pathname="", lineno=0,
                msg="hello", args=(), exc_info=None,
            )
            assert filt.filter(record) is True
            assert record.trace_id == "xyz-99"
            assert record.trace_short == "xyz-99"[:8]
        finally:
            trace_id_var.reset(token)

    def test_filter_uses_dash_when_unset(self):
        # Reset to default ""
        token = set_trace_id("")
        try:
            filt = TraceIdFilter()
            record = logging.LogRecord(
                name="t", level=logging.INFO, pathname="", lineno=0,
                msg="hello", args=(), exc_info=None,
            )
            filt.filter(record)
            assert record.trace_id == "-"
            assert record.trace_short == "-"
        finally:
            trace_id_var.reset(token)

    async def test_contextvar_propagates_across_create_task(self):
        """The whole point of contextvars: child tasks inherit the value
        without explicit threading. F8-4 relies on this for orchestrator
        → agent log correlation.
        """
        set_trace_id("propagated-trace")

        async def inner_check():
            return get_trace_id()

        # asyncio.create_task copies the parent's contextvars
        result = await asyncio.create_task(inner_check())
        assert result == "propagated-trace"
