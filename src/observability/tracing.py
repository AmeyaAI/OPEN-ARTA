"""F7-4 — Distributed tracing via OpenTelemetry.

Bridges three concerns the existing observability layer doesn't cover:

  1. **End-to-end request tracing** — instrument FastAPI so every incoming
     request becomes a parent span. Combined with the existing trace_id
     contextvar (log_context.py), each log line in the request is correlated
     to the OTel trace.
  2. **Agent-stage spans** — strategy_architect / atdd_designer / automation_engineer
     etc. expose `@traced("stage_name")` so each LLM call is its own span with
     attributes like provider, model, prompt_version.
  3. **Cross-process correlation** — when ARTA's CI calls back into /api/gates,
     the `traceparent` HTTP header propagates so a single CI run renders as one
     trace across infra components.

Configuration (env vars, all optional):
  ARTA_TRACING_ENABLED        — true/false (default: false until prod-tested)
  OTEL_SERVICE_NAME           — service name in the trace (default: arta-api)
  OTEL_EXPORTER_OTLP_ENDPOINT — OTLP collector endpoint (default: console exporter)
  OTEL_TRACES_SAMPLER         — passthrough; defaults to parentbased_traceidratio
  OTEL_TRACES_SAMPLER_ARG     — sampling ratio 0.0-1.0 (default: 1.0 in dev, 0.05 in prod)

When OpenTelemetry SDK isn't installed OR ARTA_TRACING_ENABLED is false, all
helpers become no-ops. So the rest of the codebase can use `@traced(...)`
unconditionally without a hard dependency.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

log = logging.getLogger("arta.tracing")

T = TypeVar("T", bound=Callable[..., Any])

# Lazily resolved on first use to keep import cost zero when tracing is off.
_TRACER: Any = None
_INSTALLED: bool = False


def _is_enabled() -> bool:
    return os.environ.get("ARTA_TRACING_ENABLED", "false").lower() in ("1", "true", "yes")


def _try_install() -> bool:
    """Best-effort OTel install. Returns True if tracer is now usable."""
    global _TRACER, _INSTALLED
    if _INSTALLED:
        return _TRACER is not None
    _INSTALLED = True
    if not _is_enabled():
        log.info("tracing: ARTA_TRACING_ENABLED=false — using no-op spans")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
    except ImportError:
        log.warning("tracing: opentelemetry-sdk not installed — disabling. "
                    "Install via `pip install opentelemetry-sdk opentelemetry-instrumentation-fastapi`")
        return False

    service_name = os.environ.get("OTEL_SERVICE_NAME", "arta-api")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # Pick exporter: OTLP if endpoint set, else console for local dev.
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
            log.info("tracing: OTLP exporter wired to %s", otlp_endpoint)
        except ImportError:
            exporter = ConsoleSpanExporter()
            log.warning("tracing: OTLP exporter requested but opentelemetry-exporter-otlp "
                        "not installed — falling back to console")
    else:
        exporter = ConsoleSpanExporter()
        log.info("tracing: using console exporter (set OTEL_EXPORTER_OTLP_ENDPOINT for collector)")

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("arta")
    log.info("tracing: OTel TracerProvider installed (service=%s)", service_name)
    return True


def install_fastapi_instrumentation(app: Any) -> None:
    """Wrap a FastAPI app so every request becomes a parent span.

    Idempotent — safe to call from lifespan even if tracing is disabled
    (becomes a no-op).
    """
    if not _try_install():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        log.info("tracing: FastAPI instrumentation attached")
    except ImportError:
        log.warning("tracing: opentelemetry-instrumentation-fastapi not installed — "
                    "request-level spans disabled")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Context manager wrapping a single span.

    Usage:
        with span("strategy.score_risks", model=self._model, n_reqs=len(reqs)):
            return await self._call_llm(...)

    No-op when tracing isn't installed.
    """
    if not _try_install() or _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as s:
        for k, v in attrs.items():
            try:
                s.set_attribute(k, v)
            except Exception:
                pass  # OTel rejects some types — don't break the call
        yield s


def traced(name: str | None = None, **default_attrs: Any) -> Callable[[T], T]:
    """Decorator: wrap an async function so its execution is one span.

    Usage:
        @traced("strategy.score_single", stage="risk_scoring")
        async def _score_single(self, req): ...
    """
    def decorator(func: T) -> T:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, **default_attrs):
                return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, **default_attrs):
                return func(*args, **kwargs)

        # Pick the right wrapper based on whether func is a coroutine.
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator
