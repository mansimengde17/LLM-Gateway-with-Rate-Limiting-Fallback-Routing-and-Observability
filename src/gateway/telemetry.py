"""Observability: span tracing and Prometheus style metrics.

Spans mirror what OpenTelemetry would export; the metrics registry keeps
counters and latency histograms in memory and renders the standard
Prometheus text exposition format at /metrics.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Span:
    name: str
    trace_id: str
    attributes: dict = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0

    def end(self) -> None:
        self.duration_ms = round(
            (time.perf_counter() - self.started) * 1000, 3)


class Tracer:
    def __init__(self, keep: int = 500):
        self.traces: dict[str, list[Span]] = {}
        self.keep = keep

    def start_trace(self) -> str:
        trace_id = uuid.uuid4().hex[:12]
        self.traces[trace_id] = []
        if len(self.traces) > self.keep:
            oldest = next(iter(self.traces))
            del self.traces[oldest]
        return trace_id

    def span(self, trace_id: str, name: str, **attributes) -> Span:
        span = Span(name, trace_id, attributes)
        self.traces[trace_id].append(span)
        return span


class Metrics:
    def __init__(self):
        self.counters: dict[tuple, float] = {}
        self.latencies: dict[tuple, list[float]] = {}

    def inc(self, name: str, value: float = 1, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name: str, value: float, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        self.latencies.setdefault(key, []).append(value)

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(q * len(ordered)))
        return round(ordered[index], 2)

    def summary(self) -> dict:
        latency_summary = {}
        for (name, labels), values in self.latencies.items():
            label_str = ",".join(f"{k}={v}" for k, v in labels)
            latency_summary[f"{name}{{{label_str}}}"] = {
                "count": len(values),
                "p50": self._percentile(values, 0.50),
                "p95": self._percentile(values, 0.95),
                "p99": self._percentile(values, 0.99)}
        counter_summary = {
            f"{name}{{{','.join(f'{k}={v}' for k, v in labels)}}}":
                round(value, 6)
            for (name, labels), value in self.counters.items()}
        return {"counters": counter_summary, "latency": latency_summary}

    def prometheus(self) -> str:
        lines = []
        for (name, labels), value in sorted(self.counters.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_str}}} {value}")
        for (name, labels), values in sorted(self.latencies.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            for q in (0.5, 0.95, 0.99):
                lines.append(
                    f'{name}{{quantile="{q}",{label_str}}}'
                    f" {self._percentile(values, q)}")
        return "\n".join(lines) + "\n"
