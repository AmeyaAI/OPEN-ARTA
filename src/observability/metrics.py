"""I7: Minimal Prometheus metrics endpoint.

Implements a tiny in-memory metrics registry that emits Prometheus exposition
format without requiring the prometheus_client dependency. This keeps the
Docker image slim while still giving operators a `/metrics` endpoint they can
scrape with standard Prometheus.

Metrics emitted:
  arta_llm_calls_total{provider, model, stage, status}    counter
  arta_llm_call_duration_seconds_bucket{...}              histogram (le-bucketed)
  arta_generation_jobs_active                             gauge
  arta_generation_stage_duration_seconds_sum{stage}       counter (sum)
  arta_generation_stage_duration_seconds_count{stage}     counter (count)
  arta_judge_score{provider}                              gauge (last value)
  arta_circuit_breaker_state{provider}                    gauge (0=CLOSED 1=OPEN 2=HALF_OPEN)

Usage in code:
    from src.observability.metrics import metrics
    metrics.inc("arta_llm_calls_total", labels={"provider":"ollama", "stage":"risk", "status":"ok"})
    metrics.observe("arta_generation_stage_duration_seconds", elapsed, labels={"stage":"risk"})
    metrics.set_gauge("arta_generation_jobs_active", 3)
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable

# Histogram buckets in seconds — covers fast (sub-second) to slow (15min) calls
_LATENCY_BUCKETS = (0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 900, float("inf"))


class _MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, frozenset], float] = defaultdict(float)
        self._gauges: dict[tuple[str, frozenset], float] = defaultdict(float)
        # histogram: name → label_set → list[bucket counts] + sum + count
        self._histograms: dict[tuple[str, frozenset], dict] = defaultdict(
            lambda: {"buckets": [0] * len(_LATENCY_BUCKETS), "sum": 0.0, "count": 0}
        )

    def _key(self, name: str, labels: dict | None) -> tuple[str, frozenset]:
        return (name, frozenset((labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, labels: dict | None = None) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def set_gauge(self, name: str, value: float, labels: dict | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = float(value)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        with self._lock:
            h = self._histograms[self._key(name, labels)]
            h["sum"] += value
            h["count"] += 1
            for i, le in enumerate(_LATENCY_BUCKETS):
                if value <= le:
                    h["buckets"][i] += 1

    def expose(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            # Counters
            seen_names: set[str] = set()
            for (name, labels_fs), value in sorted(self._counters.items()):
                if name not in seen_names:
                    lines.append(f"# TYPE {name} counter")
                    seen_names.add(name)
                lines.append(f"{name}{_fmt_labels(labels_fs)} {value}")

            # Gauges
            for (name, labels_fs), value in sorted(self._gauges.items()):
                if name not in seen_names:
                    lines.append(f"# TYPE {name} gauge")
                    seen_names.add(name)
                lines.append(f"{name}{_fmt_labels(labels_fs)} {value}")

            # Histograms
            for (name, labels_fs), h in sorted(self._histograms.items()):
                if name not in seen_names:
                    lines.append(f"# TYPE {name} histogram")
                    seen_names.add(name)
                base_labels = dict(labels_fs)
                for i, le in enumerate(_LATENCY_BUCKETS):
                    le_str = "+Inf" if le == float("inf") else f"{le}"
                    bucket_labels = {**base_labels, "le": le_str}
                    lines.append(f"{name}_bucket{_fmt_labels(frozenset(bucket_labels.items()))} {h['buckets'][i]}")
                lines.append(f"{name}_sum{_fmt_labels(labels_fs)} {h['sum']}")
                lines.append(f"{name}_count{_fmt_labels(labels_fs)} {h['count']}")

        return "\n".join(lines) + "\n"


def _fmt_labels(labels_fs: Iterable[tuple[str, str]]) -> str:
    items = sorted(labels_fs)
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


# Singleton
metrics = _MetricsRegistry()
